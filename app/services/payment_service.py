import io
import base64
import json
import httpx
import qrcode
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

def generate_qr_data_url(payload: str) -> str:
    """
    Renders EMVCo payload string into a base64 PNG data URL directly on the server.
    """
    if not payload:
        return ""
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=8,
        border=2,
    )
    qr.add_data(payload)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    b64 = base64.b64encode(buffer.getvalue()).decode("utf-8")
    return f"data:image/png;base64,{b64}"

async def verify_slip_with_slipok(file_bytes: bytes, filename: str, expected_amount: float) -> Dict[str, Any]:
    """
    Sends the uploaded slip image to SlipOK API for bank verification.
    """
    branch_id = get_setting("slipok_branch_id", settings.SLIPOK_BRANCH_ID).strip()
    api_key = get_setting("slipok_api_key", settings.SLIPOK_API_KEY).strip()
    promptpay_id = get_setting("promptpay_id", settings.PROMPTPAY_ID).strip()
    promptpay_name = get_setting("promptpay_name", settings.PROMPTPAY_NAME).strip()
    
    if not branch_id or not api_key:
        return {
            "success": False,
            "message": "ระบบ SlipOK ยังไม่ได้ตั้งค่า Branch ID หรือ API Key"
        }
        
    url = f"https://api.slipok.com/api/line/apikey/{branch_id}"
    headers = {
        "x-authorization": api_key
    }
    
    # Form data: send log="false" so SlipOK performs central bank verification
    # without failing on LIFF dashboard account link checks (which triggers error 1014).
    # We validate the destination account and uniqueness directly in our database.
    files = {
        "files": (filename or "slip.jpg", file_bytes, "image/jpeg")
    }
    data = {
        "amount": str(expected_amount),
        "log": "false"
    }
    
    async with httpx.AsyncClient(timeout=25.0) as client:
        try:
            resp = await client.post(url, headers=headers, files=files, data=data)
            print(f"[SlipOK DEBUG] HTTP Status: {resp.status_code}, Body: {resp.text}")
            
            if resp.status_code == 200:
                res_json = resp.json()
                if res_json.get("success"):
                    inner = res_json.get("data", {})
                    # Standard SlipOK response structure
                    if isinstance(inner, dict):
                        if "data" in inner and isinstance(inner["data"], dict) and ("transRef" in inner["data"] or "trans_ref" in inner["data"]):
                            slip_data = inner["data"]
                        else:
                            slip_data = inner
                    else:
                        slip_data = {}
                        
                    trans_ref = slip_data.get("transRef") or slip_data.get("trans_ref") or "UNKNOWN_REF"
                    
                    # 1. Amount validation
                    actual_amount_raw = slip_data.get("amount")
                    if actual_amount_raw is not None:
                        try:
                            actual_amount = float(actual_amount_raw)
                            if abs(actual_amount - expected_amount) > 0.01:
                                return {
                                    "success": False,
                                    "message": f"ยอดเงินในสลิป (฿{actual_amount:.2f}) ไม่ตรงกับราคาแพ็กเกจ (฿{expected_amount:.2f})"
                                }
                        except (ValueError, TypeError):
                            pass
                            
                    # 2. Receiver validation (if receiver info is present in slip)
                    receiver_info = slip_data.get("receiver")
                    if receiver_info and isinstance(receiver_info, dict):
                        rec_str = json.dumps(receiver_info, ensure_ascii=False).lower()
                        clean_pp = promptpay_id.replace("-", "").replace(" ", "").strip()
                        clean_name = promptpay_name.strip()
                        
                        matched = False
                        # Match PromptPay last 4 digits (e.g. 0470)
                        if len(clean_pp) >= 4 and clean_pp[-4:] in rec_str:
                            matched = True
                        if clean_pp and clean_pp in rec_str:
                            matched = True
                            
                        # Match words from promptpay_name
                        if not matched and clean_name:
                            name_words = [w.strip().lower() for w in clean_name.split() if len(w.strip()) >= 3]
                            for word in name_words:
                                if word in rec_str:
                                    matched = True
                                    break
                                    
                        # Match transliterations or Thai name
                        if not matched and "ธีรภัทร" in clean_name:
                            for nick in ["ธีรภัทร", "theeraphat", "theerapat", "teerapat", "teeraphat"]:
                                if nick in rec_str:
                                    matched = True
                                    break
                                    
                        if not matched:
                            disp = receiver_info.get("displayName") or receiver_info.get("name") or "ไม่ทราบชื่อ"
                            return {
                                "success": False,
                                "message": f"สลิปนี้ไม่ได้โอนเข้าบัญชีผู้รับของระบบ (ชื่อผู้รับในสลิป: {disp})"
                            }
                            
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
                    elif code in (1000, 1007):
                        user_msg = "ไม่พบ QR Code ในสลิป หรือรูปภาพไม่ชัดเจน กรุณาแนบสลิปธนาคารที่มี QR Code ชัดเจน"
                    elif code == 1005:
                        user_msg = "ไฟล์ไม่ใช่ไฟล์รูปภาพ กรุณาอัปโหลดไฟล์สลิป .jpg, .png หรือ .webp"
                    elif code == 1010:
                        user_msg = "ธนาคารต้นทางกำลังประมวลผล กรุณารอประมาณ 3-5 นาทีแล้วลองใหม่อีกครั้ง"
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
