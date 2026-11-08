import os
import logging
from typing import Dict, Any, List, Optional
from jinja2 import Environment, FileSystemLoader, TemplateNotFound
from services.restore_manager import RestoreManager
from services.verification import VerificationService

logger = logging.getLogger(__name__)

class ConfigManager:

    PLATFORM_TEMPLATES = {
        "mikrotik": "mikrotik_ospf.j2",
        "frr": "frr_ospf.j2",
    }

    def __init__(
        self,
        inventory_service: Any,
        backup_service: Any,
        restore_manager: RestoreManager,
        verifier: Optional[VerificationService] = None,
        template_dir: Optional[str] = None,
    ):
        self.inventory_service = inventory_service
        self.backup_service = backup_service
        self.restore_manager = restore_manager
        self.verifier = verifier or VerificationService()

        if not template_dir:
            base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            template_dir = os.path.join(base_dir, "templates")

        self.jinja_env = Environment(
            loader=FileSystemLoader(template_dir),
            trim_blocks=True,
            lstrip_blocks=True,
        )

    def render_config(
        self, platform: str, params: Dict[str, Any], template_name: Optional[str] = None
    ) -> str:

        platform_key = platform.lower()
        tpl_file = template_name or self.PLATFORM_TEMPLATES.get(platform_key)
        if not tpl_file:
            raise ValueError(f"Không hỗ trợ nền tảng '{platform}' hoặc thiếu tên template.")

        try:
            template = self.jinja_env.get_template(tpl_file)
            return template.render(**params).strip()
        except TemplateNotFound:
            raise FileNotFoundError(f"Không tìm thấy file template Jinja2: {tpl_file}")

    def dry_run(
        self,
        device_ids: List[str],
        common_params: Dict[str, Any],
        per_device_params: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
  
        per_device_params = per_device_params or {}
        previews: Dict[str, Any] = {}

        for dev_id in device_ids:
            device = self.inventory_service.get_device(dev_id)
            if not device:
                previews[dev_id] = {"status": "error", "error": "Không tìm thấy thiết bị trong Inventory."}
                continue

            merged_params = {**common_params, **per_device_params.get(dev_id, {})}
            merged_params.setdefault("hostname", dev_id)

            try:
                rendered = self.render_config(device.platform, merged_params)
                previews[dev_id] = {
                    "status": "ready",
                    "platform": device.platform,
                    "rendered_config": rendered,
                }
            except Exception as e:
                previews[dev_id] = {"status": "error", "error": str(e)}

        logger.info(f"[Dry-Run] Đã tạo bản xem trước cấu hình cho {len(device_ids)} thiết bị.")
        return {"dry_run": True, "devices": previews}

    def deploy_bulk(
        self,
        device_ids: List[str],
        common_params: Dict[str, Any],
        per_device_params: Optional[Dict[str, Dict[str, Any]]] = None,
        rollback_all_on_failure: bool = False,
    ) -> Dict[str, Any]:
     
        per_device_params = per_device_params or {}
        results: Dict[str, Any] = {}
        pre_deploy_backups: Dict[str, str] = {}
        succeeded_devices: List[str] = []

        for dev_id in device_ids:
            device = self.inventory_service.get_device(dev_id)
            if not device:
                results[dev_id] = {"status": "failed", "error": "Thiết bị không tồn tại."}
                if rollback_all_on_failure:
                    break
                continue

        
            try:
                backup_ver = self.backup_service.create_backup(device)
                pre_deploy_backups[dev_id] = backup_ver
                logger.info(f"[Bulk] Đã tạo bản backup '{backup_ver}' cho '{dev_id}' trước khi triển khai.")
            except Exception as e:
                logger.error(f"[Bulk] Không thể tạo backup trước triển khai cho '{dev_id}': {e}")
                results[dev_id] = {
                    "status": "failed",
                    "error": f"Không thể sao lưu cấu hình hiện tại trước khi triển khai: {e}",
                }
                if rollback_all_on_failure:
                    break
                continue

            merged_params = {**common_params, **per_device_params.get(dev_id, {})}
            merged_params.setdefault("hostname", dev_id)

            try:
                config_text = self.render_config(device.platform, merged_params)
            except Exception as e:
                results[dev_id] = {"status": "failed", "error": f"Lỗi sinh template: {e}"}
                if rollback_all_on_failure:
                    break
                continue

            push_success = False
            push_error = None
            try:
                push_success = device.push_config(config_text)
                if not push_success:
                    push_error = "Phương thức push_config() trả về False."
            except Exception as e:
                push_error = str(e)
              
            verify_res = {"passed": False}
            if push_success:
                expected_router_id = merged_params.get("router_id", "")
                expected_nets = [n["prefix"] for n in merged_params.get("networks", [])]
                verify_res = self.verifier.verify_ospf_deployment(
                    device=device,
                    expected_router_id=expected_router_id,
                    expected_networks=expected_nets,
                    status_monitor=self.inventory_service,
                )

            if push_success and verify_res.get("passed"):
                logger.info(f"[Bulk] Triển khai thành công trên '{dev_id}'.")
                results[dev_id] = {
                    "status": "success",
                    "backup_version": pre_deploy_backups[dev_id],
                    "verification": verify_res,
                }
                succeeded_devices.append(dev_id)
            else:
                reason = push_error or "Kiểm chứng OSPF sau triển khai thất bại."
                logger.warning(
                    f"[Bulk] Lỗi trên '{dev_id}' ({reason}). Kích hoạt tự động Rollback về '{pre_deploy_backups[dev_id]}'."
                )
                rb_res = self.restore_manager.restore_device(dev_id, pre_deploy_backups[dev_id])
                results[dev_id] = {
                    "status": "rolled_back" if rb_res["status"] == "success" else "rollback_failed",
                    "error": reason,
                    "rollback_result": rb_res,
                }
                if rollback_all_on_failure:
                    break

        any_failed = any(r.get("status") != "success" for r in results.values())
        if rollback_all_on_failure and any_failed and succeeded_devices:
            logger.warning("[Bulk] Kích hoạt Rollback hàng loạt cho các thiết bị đã triển khai trước đó.")
            for dev_id in succeeded_devices:
                rb_res = self.restore_manager.restore_device(dev_id, pre_deploy_backups[dev_id])
                results[dev_id]["status"] = "rolled_back_due_to_peer_failure"
                results[dev_id]["rollback_result"] = rb_res

        overall_status = (
            "success"
            if all(r.get("status") == "success" for r in results.values())
            else "partial_or_failed"
        )
        return {"overall_status": overall_status, "results": results}
