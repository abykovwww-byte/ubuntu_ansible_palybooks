"""Pure selection rules shared by collector and MCP; no credentials or Telegram."""

import json

GROUP_KINDS = {"group", "supergroup", "forum", "channel"}
PRIVATE_KINDS = {"private", "bot"}
KINDS = GROUP_KINDS | PRIVATE_KINDS


def private_ids(data, root=None):
    values = data.get("approved_private_ids", [])
    approval = data.get("private_approval_file")
    if approval is not None:
        if approval != "state/approved-private-peers.json" or root is None:
            raise ValueError("Invalid private_approval_file")
        path = root / approval
        if path.exists():
            receipt = json.loads(path.read_text(encoding="utf-8-sig"))
            if receipt.get("approved_by") != "owner":
                raise ValueError("Private peers require owner approval")
            values = receipt["approved_private_ids"]
    if not isinstance(values, list) or any(type(v) is not int or v <= 0 for v in values):
        raise ValueError("Invalid approved_private_ids")
    if len(set(values)) != len(values):
        raise ValueError("Duplicate approved_private_ids")
    return tuple(values)


def peer_valid(ident, kind, approved=()):
    if type(ident) is not int or ident == 0 or abs(ident) > 2**63 - 1:
        return False
    if ident > 0:
        return kind in PRIVATE_KINDS and ident in approved
    return kind is None or kind in GROUP_KINDS
