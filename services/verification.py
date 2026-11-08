import hashlib
import logging
from typing import Dict, Any, List

logger = logging.getLogger(__name__)


class VerificationService:

    @staticmethod
    def compute_hash(content: str) -> str:

        normalized = "\n".join(
            line.strip() for line in content.strip().splitlines() if line.strip()
        )
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    def verify_config_match(self, device: Any, expected_config: str) -> Dict[str, Any]:

        try:
            running_config = device.get_running_config()
            expected_hash = self.compute_hash(expected_config)
            running_hash = self.compute_hash(running_config)

            is_exact_match = expected_hash == running_hash

            expected_lines = [
                line.strip()
                for line in expected_config.splitlines()
                if line.strip() and not line.strip().startswith(("#", "!"))
            ]
            missing_lines = [
                line for line in expected_lines if line not in running_config
            ]

            passed = is_exact_match or (len(missing_lines) == 0)

            result = {
                "device_id": getattr(device, "device_id", "unknown"),
                "passed": passed,
                "exact_hash_match": is_exact_match,
                "missing_lines": missing_lines,
            }
            if passed:
                logger.info(f"[Verify] Thiết bị {result['device_id']} khớp cấu hình mong đợi.")
            else:
                logger.warning(
                    f"[Verify] Thiết bị {result['device_id']} thiếu {len(missing_lines)} dòng cấu hình."
                )
            return result

        except Exception as e:
            logger.error(f"[Verify] Lỗi khi đọc cấu hình kiểm chứng: {e}")
            return {
                "device_id": getattr(device, "device_id", "unknown"),
                "passed": False,
                "error": str(e),
            }

    def verify_ospf_deployment(
        self,
        device: Any,
        expected_router_id: str,
        expected_networks: List[str],
        status_monitor: Any = None,
    ) -> Dict[str, Any]:

        device_id = getattr(device, "device_id", "unknown")
        try:
            running_config = device.get_running_config()
            has_router_id = expected_router_id in running_config
            missing_networks = [
                net for net in expected_networks if net not in running_config
            ]
            ospf_active = True
            if status_monitor and hasattr(status_monitor, "check_ospf_status"):
                ospf_active = status_monitor.check_ospf_status(device_id)

            passed = has_router_id and (len(missing_networks) == 0) and ospf_active

            return {
                "device_id": device_id,
                "passed": passed,
                "has_router_id": has_router_id,
                "missing_networks": missing_networks,
                "ospf_active": ospf_active,
            }
        except Exception as e:
            logger.error(f"[Verify OSPF] Lỗi kiểm chứng OSPF trên {device_id}: {e}")
            return {
                "device_id": device_id,
                "passed": False,
                "error": str(e),
            }
