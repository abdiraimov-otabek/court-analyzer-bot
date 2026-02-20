"""Direct unit tests for ParserApiKadClient._map_outcome.

Covers every branch:
  - Each _DENIED_COMBINED regex (original 8 + new 4 = 12 patterns)
  - Standalone "отказ" step-3
  - Each _SATISFIED_KEYWORDS keyword
  - UNKNOWN when nothing matches
  - Priority: DENIED fires even when SATISFIED keyword is present in the same text
"""
from __future__ import annotations

import pytest

from src.domain.entities import CaseOutcome
from src.services.kad_client import ParserApiKadClient


@pytest.fixture()
def client() -> ParserApiKadClient:
    return ParserApiKadClient(base_url="http://test", api_key="key")


# ── helpers ──────────────────────────────────────────────────────────────────

def outcome(client: ParserApiKadClient, text: str) -> CaseOutcome:
    return client._map_outcome(text)


# ── DENIED: original _DENIED_COMBINED patterns ───────────────────────────────

def test_denied_otkazat_v_udovletvorenii(client):
    assert outcome(client, "Отказать в удовлетворении требования") == CaseOutcome.DENIED


def test_denied_v_udovletvorenii_otkaz_short(client):
    """Pattern index 1 — short gap (0 words)."""
    assert outcome(client, "В удовлетворении отказано") == CaseOutcome.DENIED


def test_denied_v_udovletvorenii_otkaz_long(client):
    """Pattern index 1 — long gap (6 words), previously failing with {0,5}."""
    assert outcome(client, "В удовлетворении требования о признании сделки недействительной отказано") == CaseOutcome.DENIED


def test_denied_v_udovletvorenii_otkaz_very_long(client):
    """Pattern index 1 — 8-word gap."""
    assert outcome(client, "В удовлетворении заявления конкурсного управляющего о признании недействительной сделки отказано") == CaseOutcome.DENIED


def test_denied_bez_udovletvoreniya(client):
    assert outcome(client, "Оставить без удовлетворения апелляционную жалобу") == CaseOutcome.DENIED


def test_denied_otkazat_v_iske(client):
    assert outcome(client, "Отказать в иске") == CaseOutcome.DENIED


def test_denied_v_iske_otkazano(client):
    assert outcome(client, "В иске отказано") == CaseOutcome.DENIED


def test_denied_zhalobу_ostavit_bez(client):
    assert outcome(client, "Жалобу оставить без рассмотрения") == CaseOutcome.DENIED


def test_denied_ostavit_zhalobu_bez(client):
    assert outcome(client, "Оставить жалобу без удовлетворения") == CaseOutcome.DENIED


def test_denied_otkazat_v_priznanii(client):
    assert outcome(client, "Отказать в признании сделки недействительной") == CaseOutcome.DENIED


# ── DENIED: new patterns (Bug 1) ─────────────────────────────────────────────

def test_denied_ne_podlezhit_priznaniyu(client):
    assert outcome(client, "Сделка не подлежит признанию недействительной") == CaseOutcome.DENIED


def test_denied_ne_mozhet_byt_priznana(client):
    assert outcome(client, "Сделка не может быть признана недействительной") == CaseOutcome.DENIED


def test_denied_ne_mog_byt_priznan(client):
    assert outcome(client, "Договор не мог быть признан ничтожным") == CaseOutcome.DENIED


def test_denied_otsutstvuyut_osnovaniya_dlya_priznaniya(client):
    assert outcome(client, "Отсутствуют основания для признания сделки недействительной") == CaseOutcome.DENIED


def test_denied_priznaki_ne_ustanovleny(client):
    assert outcome(client, "Признаки злоупотребления правом не установлены судом") == CaseOutcome.DENIED


def test_denied_priznaki_otsutstvuyut(client):
    assert outcome(client, "Признаки мнимости сделки отсутствуют") == CaseOutcome.DENIED


# ── DENIED: standalone "отказ" step-3 ────────────────────────────────────────

def test_denied_standalone_otkaz(client):
    assert outcome(client, "В удовлетворении жалобы отказ") == CaseOutcome.DENIED


# ── SATISFIED keywords ────────────────────────────────────────────────────────

def test_satisfied_udovletvorit(client):
    assert outcome(client, "Требования удовлетворены в полном объёме") == CaseOutcome.SATISFIED


def test_satisfied_priznat_nedeystvitelnoj(client):
    assert outcome(client, "Признать недействительной сделку должника") == CaseOutcome.SATISFIED


def test_satisfied_priznano_nezakonnym(client):
    assert outcome(client, "Решение признано незаконным") == CaseOutcome.SATISFIED


def test_satisfied_vzyskat(client):
    assert outcome(client, "Взыскать с ответчика сумму убытков") == CaseOutcome.SATISFIED


# ── Priority: DENIED beats SATISFIED ─────────────────────────────────────────

def test_denied_beats_satisfied_keyword(client):
    """DENIED_COMBINED fires before 'признать недействит' keyword."""
    text = "Отказано в удовлетворении требования о признании сделки недействительной"
    assert outcome(client, text) == CaseOutcome.DENIED


# ── Bankruptcy / Article 60 specifics ───────────────────────────────

def test_satisfied_nezakonnym_bezdeystviye(client):
    assert outcome(client, "Признать незаконным бездействие арбитражного управляющего") == CaseOutcome.SATISFIED

def test_satisfied_nenadlezhashcheye_ispolneniye(client):
    assert outcome(client, "Признать ненадлежащим исполнение обязанностей управляющим") == CaseOutcome.SATISFIED

def test_satisfied_privlech_k_otvetstvennosti(client):
    assert outcome(client, "Привлечь к административной ответственности") == CaseOutcome.SATISFIED

def test_satisfied_zhaloba_obosnovanna(client):
    assert outcome(client, "Жалоба признана обоснованной") == CaseOutcome.SATISFIED

def test_denied_proizvodstvo_prekratit(client):
    assert outcome(client, "Производство по жалобе прекратить") == CaseOutcome.DENIED

def test_denied_bez_rassmotreniya_short(client):
    assert outcome(client, "Оставить без рассмотрения") == CaseOutcome.DENIED

def test_denied_neobosnovanna(client):
    assert outcome(client, "Жалобу признать необоснованной") == CaseOutcome.DENIED

def test_denied_otsutstvuyut_pravovye_osnovaniya(client):
    assert outcome(client, "Правовые основания для удовлетворения жалобы отсутствуют") == CaseOutcome.DENIED


# ── UNKNOWN ───────────────────────────────────────────────────────────────────

def test_unknown_when_nothing_matches(client):
    assert outcome(client, "Дело передано на новое рассмотрение") == CaseOutcome.UNKNOWN
