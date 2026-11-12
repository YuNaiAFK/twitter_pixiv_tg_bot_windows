"""只有原站外部廣告標籤啟用圖片內容辨識，不以畫廊頁碼刪圖。"""
CONTENT_RULE_VERSION = "content-ocr-samples-v1"


def has_external_ads(gallery):
    tags = getattr(gallery, "tags", {}) or {}
    return any(str(value).split("|", 1)[0].strip().casefold().removeprefix("other:")
               in {"extraneous ads", "外部廣告", "外部广告"} for value in tags.get("other", []))


def rule_version(url, gallery=None):
    return CONTENT_RULE_VERSION if has_external_ads(gallery) else ""
