import os
import logging
from typing import Dict, Any, List, Optional
from services.verification import VerificationService

logger = logging.getLogger(__name__)


class RestoreManager:


    def __init__(
        self,
        backup_service: Any,
        inventory_service: Any,
        verifier: Optional[VerificationService] = None,
    ):

        self.backup_service = backup_service
        self.inventory_service = inventory_service
        self.verifier = verifier or VerificationService()

    def list_available_backups(self, device_id: str) -> List[Dict[str, Any]]:

        return self.backup_service.list_backups(device_id)

    def validate_backup(
        self, device: Any, backup_meta: Dict[str, Any], backup_content: str
    ) -> Dict[str, Any]:

        if not backup_content or not backup_content.strip():
            return {"valid": False, "reason": "Nội dung bản backup trống hoặc không tồn tại."}

        file_path = backup_meta.get("file_path")
        if file_path and not os.path.exists(file_path):
            return {"valid": False, "reason": f"File backup không tồn tại: {file_path}"}

        device_platform = getattr(device, "platform", "").lower()
        backup_platform = backup_meta.get("platform", "").lower()
        if device_platform and backup_platform and device_platform != backup_platform:
            return {
                "valid": False,
                "reason": f"Sai lệch nền tảng: Thiết bị là '{device_platform}' nhưng bản backup là '{backup_platform}'.",
            }

        expected_hash = backup_meta.get("hash")
        if expected_hash:
            actual_hash = self.verifier.compute_hash(backup_content)
            if actual_hash != expected_hash:
                return {
                    "valid": False,
                    "reason": "Mã hash của bản backup không khớp (dữ liệu có thể đã bị hỏng).",
                }

        return {"valid": True, "reason": "Bản backup hợp lệ."}

    def restore_device(self, device_id: str, version_id: str) -> Dict[str, Any]:

        logger.info(f"[Restore] Bắt đầu khôi phục thiết bị '{device_id}' về phiên bản '{version_id}'.")

        device = self.inventory_service.get_device(device_id)
        if not device:
            msg = f"Không tìm thấy thiết bị '{device_id}' trong Inventory."
            logger.error(f"[Restore] {msg}")
            return {"status": "failed", "device_id": device_id, "error": msg}

        try:
            backup_data = self.backup_service.get_backup(device_id, version_id)
            if not backup_data:
                msg = f"Không tìm thấy bản backup '{version_id}' cho thiết bị '{device_id}'."
                logger.error(f"[Restore] {msg}")
                return {"status": "failed", "device_id": device_id, "error": msg}
        except Exception as e:
            logger.error(f"[Restore] Lỗi truy xuất backup từ TV4: {e}")
            return {"status": "failed", "device_id": device_id, "error": str(e)}

        backup_meta = backup_data.get("metadata", {})
        backup_content = backup_data.get("content", "")

        validation = self.validate_backup(device, backup_meta, backup_content)
        if not validation["valid"]:
            logger.error(f"[Restore] Bản backup không hợp lệ: {validation['reason']}")
            return {
                "status": "failed",
                "device_id": device_id,
                "version_id": version_id,
                "error": validation["reason"],
            }

        try:
            restore_ok = device.restore_config(backup_content)
            if not restore_ok:
                msg = "Phương thức restore_config() trả về thất bại."
                logger.error(f"[Restore] {msg}")
                return {
                    "status": "failed",
                    "device_id": device_id,
                    "version_id": version_id,
                    "error": msg,
                }
        except Exception as e:
            logger.error(f"[Restore] Lỗi kết nối hoặc thực thi khi khôi phục trên '{device_id}': {e}")
            return {
                "status": "failed",
                "device_id": device_id,
                "version_id": version_id,
                "error": f"Lỗi khi nạp cấu hình: {str(e)}",
            }
          
        verify_result = self.verifier.verify_config_match(device, backup_content)
        if not verify_result.get("passed"):
            msg = "Cấu hình sau khôi phục không khớp với bản backup."
            logger.error(f"[Restore] {msg} Chi tiết: {verify_result}")
            return {
                "status": "failed",
                "device_id": device_id,
                "version_id": version_id,
                "verification": verify_result,
                "error": msg,
            }

        logger.info(f"[Restore] Khôi phục thành công thiết bị '{device_id}' về phiên bản '{version_id}'.")
        return {
            "status": "success",
            "device_id": device_id,
            "version_id": version_id,
            "verification": verify_result,
        }
