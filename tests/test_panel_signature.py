"""패널 변경감지 서명(_items_signature) 테스트.

재시작마다 변경사항이 없는데도 패널을 지우고 다시 올리던 버그의 회귀 방지용.
디스코드 API 는 컴포넌트를 row 순으로 묶어 반환하지만 로컬 view.children 은
정의 순서를 유지하므로, 순서가 달라도 '같은 구성'으로 판정되어야 한다.
"""

from __future__ import annotations

from types import SimpleNamespace

from bot.cogs.panel import PanelCog


def _item(custom_id: str, label: str):
    """버튼/셀렉트 대역 객체 (custom_id / label 만 가진다)."""
    return SimpleNamespace(custom_id=custom_id, label=label)


def test_items_signature_order_independent() -> None:
    """정의 순서(row 배치)만 다른 같은 구성은 동일 서명이어야 한다."""
    a = [_item("mf:a", "A"), _item("mf:b", "B"), _item("mf:c", "C")]
    b = [_item("mf:c", "C"), _item("mf:a", "A"), _item("mf:b", "B")]
    assert PanelCog._items_signature(a) == PanelCog._items_signature(b)


def test_items_signature_detects_real_change() -> None:
    """라벨이 바뀌면 변경으로 감지해야 한다."""
    a = [_item("mf:a", "A"), _item("mf:b", "B")]
    b = [_item("mf:a", "A"), _item("mf:b", "B2")]
    assert PanelCog._items_signature(a) != PanelCog._items_signature(b)


def test_items_signature_detects_added_item() -> None:
    """항목이 추가되면 변경으로 감지해야 한다."""
    a = [_item("mf:a", "A")]
    b = [_item("mf:a", "A"), _item("mf:b", "B")]
    assert PanelCog._items_signature(a) != PanelCog._items_signature(b)


def test_items_signature_ignores_label_none() -> None:
    """label 이 없어도(셀렉트 등) 오류 없이 안정적으로 비교한다."""
    a = [SimpleNamespace(custom_id="mf:v_action")]
    b = [_item("mf:v_action", "")]
    assert PanelCog._items_signature(a) == PanelCog._items_signature(b)
