"""Direct unit tests for DatabaseCaseClient._map_outcome.

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
from src.domain.outcome_mapper import OutcomeMapper


@pytest.fixture()
def mapper() -> OutcomeMapper:
    return OutcomeMapper()


# ── helpers ──────────────────────────────────────────────────────────────────

def outcome(mapper: OutcomeMapper, text: str) -> CaseOutcome:
    return mapper.map_outcome(text)


# ── DENIED: original _DENIED_COMBINED patterns ───────────────────────────────

def test_denied_otkazat_v_udovletvorenii(mapper):
    assert outcome(mapper, "Отказать в удовлетворении требования") == CaseOutcome.DENIED


def test_denied_v_udovletvorenii_otkaz_short(mapper):
    """Pattern index 1 — short gap (0 words)."""
    assert outcome(mapper, "В удовлетворении отказано") == CaseOutcome.DENIED


def test_denied_v_udovletvorenii_otkaz_long(mapper):
    """Pattern index 1 — long gap (6 words), previously failing with {0,5}."""
    assert outcome(mapper, "В удовлетворении требования о признании сделки недействительной отказано") == CaseOutcome.DENIED


def test_denied_v_udovletvorenii_otkaz_very_long(mapper):
    """Pattern index 1 — 8-word gap."""
    assert outcome(mapper, "В удовлетворении заявления конкурсного управляющего о признании недействительной сделки отказано") == CaseOutcome.DENIED


def test_denied_bez_udovletvoreniya(mapper):
    assert outcome(mapper, "Оставить без удовлетворения апелляционную жалобу") == CaseOutcome.DENIED


def test_denied_otkazat_v_iske(mapper):
    assert outcome(mapper, "Отказать в иске") == CaseOutcome.DENIED


def test_denied_v_iske_otkazano(mapper):
    assert outcome(mapper, "В иске отказано") == CaseOutcome.DENIED


def test_denied_zhalobу_ostavit_bez(mapper):
    assert outcome(mapper, "Жалобу оставить без рассмотрения") == CaseOutcome.DENIED


def test_denied_ostavit_zhalobu_bez(mapper):
    assert outcome(mapper, "Оставить жалобу без удовлетворения") == CaseOutcome.DENIED


def test_denied_otkazat_v_priznanii(mapper):
    assert outcome(mapper, "Отказать в признании сделки недействительной") == CaseOutcome.DENIED


# ── DENIED: new patterns (Bug 1) ─────────────────────────────────────────────

def test_denied_ne_podlezhit_priznaniyu(mapper):
    assert outcome(mapper, "Сделка не подлежит признанию недействительной") == CaseOutcome.DENIED


def test_denied_ne_mozhet_byt_priznana(mapper):
    assert outcome(mapper, "Сделка не может быть признана недействительной") == CaseOutcome.DENIED


def test_denied_ne_mog_byt_priznan(mapper):
    assert outcome(mapper, "Договор не мог быть признан ничтожным") == CaseOutcome.DENIED


def test_denied_otsutstvuyut_osnovaniya_dlya_priznaniya(mapper):
    assert outcome(mapper, "Отсутствуют основания для признания сделки недействительной") == CaseOutcome.DENIED


def test_denied_priznaki_ne_ustanovleny(mapper):
    assert outcome(mapper, "Признаки злоупотребления правом не установлены судом") == CaseOutcome.DENIED


def test_denied_priznaki_otsutstvuyut(mapper):
    assert outcome(mapper, "Признаки мнимости сделки отсутствуют") == CaseOutcome.DENIED


# ── DENIED: standalone "отказ" step-3 ────────────────────────────────────────

def test_denied_standalone_otkaz(mapper):
    assert outcome(mapper, "В удовлетворении жалобы отказ") == CaseOutcome.DENIED


# ── SATISFIED keywords ────────────────────────────────────────────────────────

def test_satisfied_udovletvorit(mapper):
    assert outcome(mapper, "Требования удовлетворены в полном объёме") == CaseOutcome.SATISFIED


def test_satisfied_priznat_nedeystvitelnoj(mapper):
    assert outcome(mapper, "Признать недействительной сделку должника") == CaseOutcome.SATISFIED


def test_satisfied_priznano_nezakonnym(mapper):
    assert outcome(mapper, "Решение признано незаконным") == CaseOutcome.SATISFIED


def test_satisfied_vzyskat(mapper):
    assert outcome(mapper, "Взыскать с ответчика сумму убытков") == CaseOutcome.SATISFIED


# ── Priority: DENIED beats SATISFIED ─────────────────────────────────────────

def test_denied_beats_satisfied_keyword(mapper):
    """DENIED_COMBINED fires before 'признать недействит' keyword."""
    text = "Отказано в удовлетворении требования о признании сделки недействительной"
    assert outcome(mapper, text) == CaseOutcome.DENIED


# ── Bankruptcy / Article 60 specifics ───────────────────────────────

def test_satisfied_nezakonnym_bezdeystviye(mapper):
    assert outcome(mapper, "Признать незаконным бездействие арбитражного управляющего") == CaseOutcome.SATISFIED

def test_satisfied_nenadlezhashcheye_ispolneniye(mapper):
    assert outcome(mapper, "Признать ненадлежащим исполнение обязанностей управляющим") == CaseOutcome.SATISFIED

def test_satisfied_privlech_k_otvetstvennosti(mapper):
    assert outcome(mapper, "Привлечь к административной ответственности") == CaseOutcome.SATISFIED

def test_satisfied_zhaloba_obosnovanna(mapper):
    assert outcome(mapper, "Жалоба признана обоснованной") == CaseOutcome.SATISFIED

def test_denied_proizvodstvo_prekratit(mapper):
    assert outcome(mapper, "Производство по жалобе прекратить") == CaseOutcome.DENIED

def test_denied_bez_rassmotreniya_short(mapper):
    assert outcome(mapper, "Оставить без рассмотрения") == CaseOutcome.DENIED

def test_denied_neobosnovanna(mapper):
    assert outcome(mapper, "Жалобу признать необоснованной") == CaseOutcome.DENIED

def test_denied_otsutstvuyut_pravovye_osnovaniya(mapper):
    assert outcome(mapper, "Правовые основания для удовлетворения жалобы отсутствуют") == CaseOutcome.DENIED


# ── UNKNOWN ───────────────────────────────────────────────────────────────────

def test_unknown_when_nothing_matches(mapper):
    assert outcome(mapper, "Дело передано на новое рассмотрение") == CaseOutcome.UNKNOWN
