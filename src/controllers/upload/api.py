"""
src/controllers/upload/api.py
Web API Router for statement uploads and confirmation endpoints.
"""
from __future__ import annotations

import uuid
import hashlib
import logging
from pathlib import Path
from typing import Annotated
import aiofiles
import PyPDF2
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from src.instances.config import get_settings
from src.instances.database import get_db
from src.dbs.models import User
from src.dbs.repository import UploadHistoryRepository
from src.middleware.auth import verify_token
from src.controllers.upload.model import StatementKind, ConfirmStatementRequest
from src.services.statement.service import StatementService
from src.services.reports.income_statement import IncomeStatementService
from src.services.reports.balance_sheet import BalanceSheetService
from src.utils.image_utils import stitch_images_vertically

log = logging.getLogger(__name__)

router = APIRouter(prefix="/upload", tags=["upload"])
settings = get_settings()


@router.post("/parse")
async def upload_and_parse_statement(
    file: Annotated[list[UploadFile], File(description="PDF statement, or one or more screenshots of one statement")],
    kind: Annotated[StatementKind, Form(description="bank | credit_card | brokerage | einvoice")],
    account_code: Annotated[str | None, Form()] = None,
    password: Annotated[str | None, Form(description="Password to decrypt PDF if encrypted")] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(verify_token),
):
    """
    Parse a PDF statement using Gemini, returning the JSON structure without writing to the database.
    Several screenshots of the same statement can be uploaded at once; they are stitched in order.
    """
    history_repo = UploadHistoryRepository(db, current_user.id)

    image_exts = (".png", ".jpg", ".jpeg", ".webp", ".heic", ".heif")
    valid_exts = (".pdf", ".csv") + image_exts if kind == "einvoice" else (".pdf",) + image_exts
    for upload in file:
        if not upload.filename or not upload.filename.lower().endswith(valid_exts):
            raise HTTPException(status_code=400, detail=f"Only {', '.join(valid_exts)} files are accepted")

    max_bytes = settings.max_upload_size_mb * 1024 * 1024
    if len(file) > 1:
        names = [upload.filename.lower() for upload in file]
        if not all(n.endswith(image_exts) for n in names):
            raise HTTPException(status_code=400, detail="一次上傳多個檔案僅支援圖片（同一份對帳單的截圖）")
        if any(n.endswith((".heic", ".heif")) for n in names):
            raise HTTPException(status_code=400, detail="多張上傳暫不支援 HEIC，請改用 PNG / JPG 截圖")
        images = [await upload.read() for upload in file]
        if sum(len(b) for b in images) > max_bytes:
            raise HTTPException(status_code=413, detail=f"Files too large (max {settings.max_upload_size_mb} MB in total)")
        # Same screenshots in the same order -> same hash, so re-uploads are still caught as duplicates
        file_hash = hashlib.sha256(b"".join(hashlib.sha256(b).digest() for b in images)).hexdigest()
        try:
            contents = stitch_images_vertically(images)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"無法讀取圖片：{exc}")
        filename = f"{file[0].filename} 等 {len(file)} 張"[:255]
        suffix = ".png"
    else:
        contents = await file[0].read()
        if len(contents) > max_bytes:
            raise HTTPException(status_code=413, detail=f"File too large (max {settings.max_upload_size_mb} MB)")
        file_hash = hashlib.sha256(contents).hexdigest()
        filename = file[0].filename
        suffix = Path(filename).suffix.lower()

    existing = await history_repo.get_by_hash(file_hash)
    if existing:
        raise HTTPException(status_code=409, detail="這份檔案已經上傳過囉！ (Duplicate File)")

    upload_dir = Path(settings.upload_dir) / "statements"
    upload_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = upload_dir / f"{uuid.uuid4()}{suffix}"

    async with aiofiles.open(tmp_path, "wb") as f:
        await f.write(contents)

    if suffix == ".pdf":
        try:
            reader = PyPDF2.PdfReader(str(tmp_path))
            if reader.is_encrypted:
                if not password:
                    raise HTTPException(status_code=400, detail="This PDF file is encrypted. Password is required.")
                reader.decrypt(password)
                try:
                    _ = reader.pages[0]
                except Exception:
                    raise HTTPException(status_code=400, detail="Failed to decrypt PDF. Incorrect password.")

                writer = PyPDF2.PdfWriter()
                for page in reader.pages:
                    writer.add_page(page)
                with open(tmp_path, "wb") as f:
                    writer.write(f)
        except HTTPException:
            tmp_path.unlink(missing_ok=True)
            raise
        except Exception as exc:
            tmp_path.unlink(missing_ok=True)
            raise HTTPException(status_code=400, detail=f"Invalid or corrupted PDF file: {exc}")

    try:
        service = StatementService(db, current_user.id)
        parsed_data = await service.parse_statement(tmp_path, kind, filename)
        if account_code:
            parsed_data["account_code"] = account_code
        return {
            "status": "ok",
            "filename": filename,
            "file_hash": file_hash,
            "parsed_data": parsed_data
        }
    except Exception as exc:
        log.error(f"Parsing failed: {exc}")
        raise HTTPException(status_code=500, detail=f"Parsing failed: {str(exc)}")
    finally:
        tmp_path.unlink(missing_ok=True)


@router.post("/confirm")
async def confirm_statement(
    body: ConfirmStatementRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(verify_token),
):
    """
    Save the reviewed and edited statement payload to database and trigger recomputations.
    """
    history_repo = UploadHistoryRepository(db, current_user.id)
    
    existing = await history_repo.get_by_hash(body.file_hash)
    if existing:
        raise HTTPException(status_code=409, detail="這份檔案已經上傳過囉！ (Duplicate File)")
        
    history = await history_repo.create(
        filename=body.filename,
        kind=body.kind,
        status="success",
        message="解析與確認完成",
        file_hash=body.file_hash
    )
    
    try:
        service = StatementService(db, current_user.id)
        data = body.model_dump()
        result = await service.save_parsed_statement(data, upload_history_id=history.id)
        
        is_service = IncomeStatementService(db, current_user.id)
        bs_service = BalanceSheetService(db, current_user.id)
        
        await is_service.compute(body.period_year, body.period_month)
        await bs_service.compute(body.period_year, body.period_month)
        await db.commit()
        
        return {"status": "ok", "period": f"{body.period_year}-{body.period_month:02d}-01", **result}
    except Exception as exc:
        import traceback
        log.error(f"Confirmation failed traceback: {traceback.format_exc()}")
        await history_repo.delete(history.id)
        await db.rollback()
        raise HTTPException(status_code=500, detail=f"Confirmation failed: {str(exc)}")


@router.get("/history")
async def get_upload_history(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(verify_token),
):
    """Get recent upload histories."""
    repo = UploadHistoryRepository(db, current_user.id)
    histories = await repo.list_recent(limit=50)
    return [
        {
            "id": h.id,
            "filename": h.filename,
            "kind": h.kind,
            "status": h.status,
            "message": h.message,
            "created_at": h.created_at.isoformat(),
        }
        for h in histories
    ]


@router.delete("/history/{history_id}")
async def delete_upload_history(
    history_id: int, 
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(verify_token),
):
    """Delete an upload history record to allow re-upload."""
    repo = UploadHistoryRepository(db, current_user.id)
    success = await repo.delete(history_id)
    if not success:
        raise HTTPException(status_code=404, detail="Upload history not found")
    return {"status": "ok"}
