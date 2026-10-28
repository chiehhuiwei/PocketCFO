"""
src/instances/gemini.py
Google Gemini client singleton with retry logic.
"""
from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

from google import genai
from google.genai import types
from tenacity import retry, stop_after_attempt, wait_exponential

from src.instances.config import get_settings

settings = get_settings()

_client: genai.Client | None = None


def get_gemini_client() -> genai.Client:
    global _client
    if _client is None:
        _client = genai.Client(api_key=settings.gemini_api_key)
    return _client


_SLICE_ASPECT = 1.4  # slice height / width
_SLICE_OVERLAP_RATIO = 0.08


def _slice_tall_image(file_bytes: bytes) -> list[bytes] | None:
    """
    Split a very tall screenshot (e.g. a full-page web statement) into overlapping slices.
    Gemini downscales a whole image to a fixed budget, which makes text in long screenshots unreadable.
    Returns None when the image is not tall enough to need slicing or cannot be decoded.
    """
    import io
    import logging
    from PIL import Image

    try:
        img = Image.open(io.BytesIO(file_bytes))
        img.load()
    except Exception as e:
        logging.getLogger(__name__).warning(f"Could not decode image for slicing: {e}")
        return None

    width, height = img.size
    slice_h = int(width * _SLICE_ASPECT)
    if height <= slice_h * 1.5:
        return None

    overlap = int(slice_h * _SLICE_OVERLAP_RATIO)
    slices = []
    top = 0
    while top < height:
        bottom = min(top + slice_h, height)
        buf = io.BytesIO()
        img.crop((0, top, width, bottom)).convert("RGB").save(buf, format="PNG")
        slices.append(buf.getvalue())
        if bottom == height:
            break
        top = bottom - overlap
    return slices


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    reraise=True,
)
async def extract_from_pdf(pdf_path: Path, prompt: str) -> str:
    """
    Send a PDF or Image to Gemini and return the text response.
    """
    import logging
    log = logging.getLogger(__name__)

    suffix = pdf_path.suffix.lower()
    mime_type = "application/pdf"
    is_image = False
    
    if suffix in (".png", ".jpg", ".jpeg", ".webp", ".heic", ".heif"):
        is_image = True
        if suffix == ".png":
            mime_type = "image/png"
        elif suffix in (".jpg", ".jpeg"):
            mime_type = "image/jpeg"
        elif suffix == ".webp":
            mime_type = "image/webp"
        elif suffix == ".heic":
            mime_type = "image/heic"
        elif suffix == ".heif":
            mime_type = "image/heif"

    client = get_gemini_client()
    file_bytes = pdf_path.read_bytes()

    image_slices = _slice_tall_image(file_bytes) if is_image else None
    if image_slices:
        contents = [types.Part.from_bytes(data=b, mime_type="image/png") for b in image_slices]
        contents.append(
            f"The {len(image_slices)} images above are consecutive vertical slices (top to bottom) of ONE long "
            "statement image. Each slice repeats a thin strip from the bottom of the previous slice, so the first "
            "row(s) at the TOP of a slice may duplicate the LAST row(s) at the BOTTOM of the previous slice — count "
            "such boundary rows once. Rows that look identical but are inside the same slice (or are not at a slice "
            "boundary) are separate transactions and MUST all be kept."
        )
    else:
        contents = [
            types.Part.from_bytes(data=file_bytes, mime_type=mime_type),
        ]

    # Only attempt local text extraction for PDFs
    if not is_image:
        extracted_text = ""
        try:
            import pdfplumber
            with pdfplumber.open(pdf_path) as pdf:
                pages_text = []
                for i, page in enumerate(pdf.pages):
                    text = page.extract_text()
                    if text:
                        pages_text.append(f"--- PAGE {i+1} ---\n{text}")
                extracted_text = "\n\n".join(pages_text)
        except Exception as e:
            log.warning(f"Failed to extract text from PDF using pdfplumber: {e}")
            try:
                import PyPDF2
                with open(pdf_path, "rb") as f:
                    reader = PyPDF2.PdfReader(f)
                    pages_text = []
                    for i, page in enumerate(reader.pages):
                        text = page.extract_text()
                        if text:
                            pages_text.append(f"--- PAGE {i+1} ---\n{text}")
                    extracted_text = "\n\n".join(pages_text)
            except Exception as e2:
                log.warning(f"Failed to extract text using PyPDF2: {e2}")

        if extracted_text.strip():
            contents.append(f"Here is the locally extracted text content of the PDF statement to ensure 100% data and numerical accuracy:\n\n{extracted_text}")

    from datetime import date
    contents.append(
        f"Today's date is {date.today().isoformat()}. If transaction dates omit the year (e.g. `08/26`), take the "
        "year from the statement period shown in the document; if no year is shown anywhere, use the most recent "
        "year that does not put the date in the future."
    )
    contents.append(prompt)

    response = await client.aio.models.generate_content(
        model=settings.gemini_model,
        contents=contents,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.0,
        ),
    )
    return response.text


async def extract_structured(pdf_path: Path, prompt: str) -> dict[str, Any]:
    """Extract and parse JSON from a PDF via Gemini."""
    import json

    raw = await extract_from_pdf(pdf_path, prompt)
    raw = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    parsed = json.loads(raw)
    if isinstance(parsed, list):
        if len(parsed) > 0:
            return parsed[0]
        return {}
    return parsed


def is_rate_limit_error(exc: Exception) -> bool:
    err_str = str(exc).lower()
    return any(k in err_str for k in ["429", "rate limit", "resourceexhausted", "quota", "too many requests"])


async def generate_content_with_fallback(
    contents: list[Any],
    config: types.GenerateContentConfig,
    primary_model: str | None = None,
) -> tuple[Any, str]:
    """
    Attempts generation with primary_model first.
    If a rate limit error (429/quota) occurs, automatically fails over down the fallback model chain.
    Returns (response, used_model_name).
    """
    import logging
    log = logging.getLogger(__name__)

    client = get_gemini_client()
    raw_fallbacks = [m.strip() for m in settings.fallback_models.split(",") if m.strip()]
    
    primary = primary_model or settings.gemini_model
    candidates: list[str] = []
    for m in [primary] + raw_fallbacks + ["gemini-3.1-flash-lite", "gemma-4-26b-it", "gemma-4-31b-it", "gemini-2.5-flash", "gemini-2.5-pro"]:
        if m and m not in candidates:
            candidates.append(m)

    last_error: Exception | None = None
    for model_name in candidates:
        try:
            log.info(f"Attempting generate_content with model: {model_name}")
            response = await client.aio.models.generate_content(
                model=model_name,
                contents=contents,
                config=config,
            )
            return response, model_name
        except Exception as exc:
            last_error = exc
            if is_rate_limit_error(exc):
                log.warning(f"Rate limit / quota error on model '{model_name}': {exc}. Failover to next fallback model...")
            else:
                log.error(f"Error on model '{model_name}': {exc}. Trying next candidate...")

    if last_error:
        raise last_error
    raise RuntimeError("No model candidates available for generation.")


async def generate_content_stream_with_fallback(
    contents: list[Any],
    config: types.GenerateContentConfig,
    primary_model: str | None = None,
):
    """
    Attempts generate_content_stream with primary_model first.
    If a rate limit error occurs, automatically fails over to next candidate.
    Yields (chunk, used_model_name).
    """
    import logging
    log = logging.getLogger(__name__)

    client = get_gemini_client()
    raw_fallbacks = [m.strip() for m in settings.fallback_models.split(",") if m.strip()]
    
    primary = primary_model or settings.gemini_model
    candidates: list[str] = []
    for m in [primary] + raw_fallbacks + ["gemini-3.1-flash-lite", "gemma-4-26b-it", "gemma-4-31b-it", "gemini-2.5-flash", "gemini-2.5-pro"]:
        if m and m not in candidates:
            candidates.append(m)

    last_error: Exception | None = None
    for model_name in candidates:
        try:
            log.info(f"Attempting generate_content_stream with model: {model_name}")
            response_stream = await client.aio.models.generate_content_stream(
                model=model_name,
                contents=contents,
                config=config,
            )
            async for chunk in response_stream:
                yield chunk, model_name
            return
        except Exception as exc:
            last_error = exc
            if is_rate_limit_error(exc):
                log.warning(f"Rate limit / quota error on stream with model '{model_name}': {exc}. Failover to next fallback model...")
            else:
                log.error(f"Streaming error on model '{model_name}': {exc}. Trying next candidate...")

    if last_error:
        raise last_error
    raise RuntimeError("No model candidates available for streaming.")
