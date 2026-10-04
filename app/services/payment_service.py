import json
import httpx
from typing import Optional, Dict, Any
from app.core.config import settings
from app.core.database import db_session, get_setting

def crc16_ccitt(data: str) -> str:
    crc = 0xFFFF
    for byte in data.encode('ascii'):
        crc ^= (byte << 8)
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return f"{crc:04X}"

def generate_promptpay_payload(target: str, amount: Optional[float] = None) -> str:
    """
    Generates standard EMVCo Thai PromptPay QR Code payload.
    Supports 10-digit mobile phone or 13-digit National ID / Tax ID.
    """
    if not target:
        return ""
        
    cleaned = target.replace("-", "").replace(" ", "").strip()
    if len(cleaned) == 10 and cleaned.startswith("0"):
        # Mobile Phone: 08x-xxx-xxxx -> 00668xxxxxxxx
        formatted_target = "0066" + cleaned[1:]
        sub_id = "01"
    elif len(cleaned) == 13:
        # National ID / Tax ID
        formatted_target = cleaned
        sub_id = "02"
    else:
        formatted_target = cleaned
        sub_id = "03"
        
    tag29_sub = f"{sub_id}{len(formatted_target):02d}{formatted_target}"
    tag29 = f"0016A000000677010111{tag29_sub}"
    
    parts = [
        "000201",
        "010212" if amount is not None else "010211",
        f"29{len(tag29):02d}{tag29}",
        "5802TH",
        "5303764"
    ]
    if amount is not None:
        amt_str = f"{amount:.2f}"
        parts.append(f"54{len(amt_str):02d}{amt_str}")
    
    raw = "".join(parts) + "6304"
    return raw + crc16_ccitt(raw)

async def verify_slip_with_slipok(file_bytes: bytes, filename: str, expected_amount: float) -> Dict[str, Any]:
    """
    Sends the uploaded slip image to SlipOK API for bank verification.
    """
    branch_id = get_setting("slipok_branch_id", settings.SLIPOK_BRANCH_ID).strip()
    api_key = get_setting("slipok_api_key", settings.SLIPOK_API_KEY).strip()
    
    if not branch_id or not api_key:
        return {
            "success": False,
            "message": "ระบบ SlipOK ยังไม่ได้ตั้งค่า Branch ID หรือ API Key"
        }
        
    url = f"https://api.slipok.com/api/line/apikey/{branch_id}"
    headers = {
        "x-authorization": api_key
    }
    
    # Form data
    files = {
        "files": (filename or "slip.jpg", file_bytes, "image/jpeg")
    }
    data = {
        "amount": str(expected_amount),
        "log": "true"
    }
    
    async with httpx.AsyncClient(timeout=25.0) as client:
        try:
            resp = await client.post(url, headers=headers, files=files, data=data)
            
            if resp.status_code == 200:
                res_json = resp.json()
                if res_json.get("success"):
                    inner = res_json.get("data", {})
                    # Standard SlipOK response structure
                    slip_data = inner.get("data", inner) if isinstance(inner, dict) else {}
                    trans_ref = slip_data.get("transRef") or slip_data.get("trans_ref") or "UNKNOWN_REF"
                    return {
                        "success": True,
                        "trans_ref": trans_ref,
                        "data": slip_data,
                        "raw": res_json
                    }
                else:
                    return {
                        "success": False,
                        "message": res_json.get("message", "การตรวจสอบสลิปไม่สำเร็จ")
                    }
            else:
                try:
                    err_json = resp.json()
                    code = err_json.get("code")
                    msg = err_json.get("message", "")
                    
                    if code == 1012:
                        user_msg = "สลิปนี้ถูกใช้งานไปแล้ว ไม่สามารถใช้ซ้ำได้"
                    elif code == 1013:
                        user_msg = f"ยอดเงินในสลิปไม่ตรงกับราคาแพ็กเกจ (ต้องเป็น ฿{expected_amount:.2f})"
                    elif code == 1014:
                        user_msg = "สลิปนี้ไม่ได้โอนเข้าบัญชีผู้รับของระบบ"
                    elif code == 1000:
                        user_msg = "ไม่พบ QR Code ในสลิป หรือรูปภาพไม่ชัดเจน กรุณาแนบสลิปธนาคารที่มี QR Code ชัดเจน"
                    else:
                        user_msg = msg or f"เกิดข้อผิดพลาดในการตรวจสอบสลิป (Code: {code})"
                        
                    return {"success": False, "message": user_msg, "code": code}
                except Exception:
                    return {
                        "success": False,
                        "message": f"เซิร์ฟเวอร์ตรวจสอบสลิปตอบกลับผิดพลาด (HTTP {resp.status_code})"
                    }
        except httpx.TimeoutException:
            return {
                "success": False,
                "message": "การเชื่อมต่อกับระบบตรวจสอบสลิปหมดเวลา (Timeout) กรุณาลองใหม่อีกครั้ง"
            }
        except Exception as e:
            return {
                "success": False,
                "message": f"เกิดข้อผิดพลาดในการเชื่อมต่อ: {str(e)}"
            }

def is_trans_ref_used(trans_ref: str) -> bool:
    with db_session() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM payment_transactions WHERE trans_ref = ?", (trans_ref,))
        return cursor.fetchone() is not None

def record_payment_transaction(user_id: str, pass_type: str, amount: float, trans_ref: str, payment_method: str = "slipok") -> bool:
    with db_session() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM payment_transactions WHERE trans_ref = ?", (trans_ref,))
        if cursor.fetchone():
            return False  # Already exists!
        cursor.execute("""
        INSERT INTO payment_transactions (user_id, pass_type, amount, trans_ref, payment_method)
        VALUES (?, ?, ?, ?, ?)
        """, (user_id, pass_type, amount, trans_ref, payment_method))
        return True

def get_recent_payments(limit: int = 20) -> list:
    with db_session() as conn:
        cursor = conn.cursor()
        cursor.execute("""
        SELECT p.*, u.username, u.avatar_url
        FROM payment_transactions p
        LEFT JOIN users u ON p.user_id = u.id
        ORDER BY p.id DESC
        LIMIT ?
        """, (limit,))
        return [dict(r) for r in cursor.fetchall()]
