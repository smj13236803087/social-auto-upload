"""Normalize multi-platform upload targets for subscriptions / queues / download-publish."""

from __future__ import annotations

from web_mvp.bili_partitions import DEFAULT_BILIBILI_TID

PLATFORMS = ("douyin", "kuaishou", "bilibili", "tencent")


def parse_targets_payload(
    data: dict | None,
    *,
    default_tid: int | None = None,
) -> list[dict]:
    """Parse targets from API JSON. Accepts `targets` list or legacy platform/account fields."""
    data = data or {}
    tid_default = int(default_tid or data.get("tid") or DEFAULT_BILIBILI_TID)
    out: list[dict] = []
    raw = data.get("targets")
    if isinstance(raw, list) and raw:
        for item in raw:
            if not isinstance(item, dict):
                continue
            platform = (item.get("platform") or item.get("upload_platform") or "").strip()
            account = (item.get("account") or item.get("upload_account") or "").strip()
            if platform not in PLATFORMS or not account:
                continue
            out.append(
                {
                    "platform": platform,
                    "account": account,
                    "tid": int(item.get("tid") or tid_default),
                }
            )
    if out:
        return _dedupe(out)

    platform = (data.get("upload_platform") or data.get("platform") or "").strip()
    account = (data.get("upload_account") or data.get("account") or "").strip()
    if platform in PLATFORMS and account:
        return [
            {
                "platform": platform,
                "account": account,
                "tid": tid_default,
            }
        ]
    return []


def targets_from_record(record: dict | None) -> list[dict]:
    """Read targets from a stored subscription/queue record (with legacy fallback)."""
    return parse_targets_payload(record or {})


def targets_summary(targets: list[dict]) -> str:
    labels = {"douyin": "抖音", "kuaishou": "快手", "bilibili": "B站"}
    if not targets:
        return "未设置目标"
    parts = []
    for t in targets:
        plat = labels.get(t.get("platform"), t.get("platform"))
        parts.append(f"{plat}/{t.get('account')}")
    return "、".join(parts)


def _dedupe(targets: list[dict]) -> list[dict]:
    seen: set[tuple[str, str]] = set()
    out: list[dict] = []
    for t in targets:
        key = (t["platform"], t["account"])
        if key in seen:
            continue
        seen.add(key)
        out.append(t)
    return out
