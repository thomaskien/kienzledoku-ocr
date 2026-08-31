"""APS document-reference find/update client."""

from __future__ import annotations

import copy
from typing import Any

from .errors import ApsFindError, ApsUpdateError, HttpRequestError
from .http_client import HttpClient
from .models import DocumentSnapshot


def _ref_object_id(ref: Any) -> str:
    if not isinstance(ref, dict):
        return ""
    value = ref.get("objectId")
    if isinstance(value, dict):
        value = value.get("id")
    return str(value) if value is not None else ""


def _ref_revision(ref: Any, fallback: int) -> int:
    if isinstance(ref, dict):
        value = ref.get("revision")
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    return fallback


class ApsClient:
    def __init__(self, http: HttpClient, aps_base_url: str) -> None:
        self._http = http
        self._base = aps_base_url.rstrip("/") + "/praxis/verweis/dokumentverweis"

    def find(self, object_id: str, revision: int) -> DocumentSnapshot:
        body = {
            "kontext": None,
            "dokumentverweisRef": {
                "objectId": {"id": object_id},
                "revision": revision,
            },
        }
        try:
            result = self._http.request(
                "POST", f"{self._base}/find", json_body=body
            )
            data = result.json()
        except HttpRequestError as exc:
            raise ApsFindError(str(exc)) from exc

        if not isinstance(data, dict) or data.get("successful") is not True:
            raise ApsFindError("APS find meldet keinen erfolgreichen Aufruf")
        dto = data.get("dokumentverweisTO")
        if not isinstance(dto, dict):
            raise ApsFindError("APS find enthält kein dokumentverweisTO")

        ref = dto.get("ref")
        found_object_id = _ref_object_id(ref)
        if not found_object_id:
            raise ApsFindError("APS-Dokumentverweis enthält keine objectId")
        if found_object_id != object_id:
            raise ApsFindError("APS find lieferte eine andere objectId")

        text = dto.get("text")
        if text is None:
            text = ""
        if not isinstance(text, str):
            raise ApsFindError("APS-Dokumenttext ist kein String")

        return DocumentSnapshot(
            dto=dto,
            object_id=found_object_id,
            revision=_ref_revision(ref, revision),
            text=text,
        )

    def update_text(self, current: DocumentSnapshot, new_text: str) -> dict[str, Any]:
        document = copy.deepcopy(current.dto)
        document["text"] = new_text
        body = {
            "kontext": None,
            "uploadToken": None,
            "dokumentverweis": document,
            "neuerEintrag": False,
        }
        try:
            result = self._http.request(
                "POST", f"{self._base}/update", json_body=body
            )
            data = result.json()
        except HttpRequestError as exc:
            raise ApsUpdateError(str(exc)) from exc

        if not isinstance(data, dict) or data.get("successful") is not True:
            raise ApsUpdateError("APS update meldet keinen erfolgreichen Aufruf")
        return data
