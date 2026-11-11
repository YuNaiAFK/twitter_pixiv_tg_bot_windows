"""讀取 gallery-dl 寫出的 Twitter 敏感內容標記。"""
import json


def sensitive_media(files):
    unknown = False
    for path in files:
        try:
            data = json.loads(path.with_name(path.name + ".json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            unknown = True
            continue
        if not isinstance(data, dict):
            unknown = True
            continue
        if data.get("sensitive") is True or data.get("possibly_sensitive") is True or data.get("sensitive_flags"):
            return True
        if data.get("sensitive") is not False and data.get("possibly_sensitive") is not False:
            unknown = True
    # 舊資料／來源未提供分級時仍加遮罩，避免未知內容直接曝光。
    return unknown or not files
