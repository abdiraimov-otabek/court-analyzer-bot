from src.domain.reason_extractor import ReasonExtractor


def test_reason_extractor_finds_patterns():
    extractor = ReasonExtractor()
    text = "Суд установил пропуск срока и недоказанность обстоятельств."
    reasons = extractor.extract(text)

    assert any("пропуск срока" in r for r in reasons)
    assert "недоказанность обстоятельств" in reasons


def test_reason_extractor_finds_bankruptcy_specific_patterns_and_articles():
    extractor = ReasonExtractor()
    text = "Жалоба на бездействие АУ по оспариванию сделки по ст. 61.2 Закона о банкротстве."
    reasons = extractor.extract(text)

    assert any("61.2" in r for r in reasons)


def test_reason_extractor_finds_legal_grounds_for_top_reasons():
    extractor = ReasonExtractor()
    text = "Сделка с предпочтением, причинение вреда кредиторам и неравноценное исполнение."
    reasons = extractor.extract(text)

    assert any("предпочтен" in r for r in reasons)
    assert any("причинение вреда кредитор" in r for r in reasons)
    assert any("неравноценн" in r for r in reasons)


def test_reason_extractor_ignores_generic_invalidity_topic():
    extractor = ReasonExtractor()
    text = "Заявлено требование о признании сделки недействительной."
    reasons = extractor.extract(text)

    assert any("недействит" in r for r in reasons)


def test_reason_extractor_returns_generic_fallback_for_unrecognized_text():
    extractor = ReasonExtractor()
    text = "Служебная заметка без содержательных правовых оснований."
    reasons = extractor.extract(text)

    assert reasons == ("оценка обстоятельств дела",)


# ── Bug-fix regression tests ──────────────────────────────────────────────────

def test_krupny_ushherb_does_not_produce_krupnaya_sdelka():
    """Bug 4: 'крупный ущерб' must NOT produce label 'крупная сделка'."""
    extractor = ReasonExtractor()
    reasons = extractor.extract("Причинён крупный ущерб кредиторам.")
    assert "крупная сделка" not in reasons


def test_krupnoy_sdelki_produces_krupnaya_sdelka():
    """Bug 4: inflected form 'крупной сделки' MUST produce label 'крупная сделка'."""
    extractor = ReasonExtractor()
    reasons = extractor.extract("Оспаривание крупной сделки в порядке ст.46 Закона об ООО.")
    assert "крупная сделка" in reasons


def test_nenadlezhaschy_otvetshik_produces_only_one_label():
    """Bug 3: 'ненадлежащий ответчик' must produce ONLY the procedural label."""
    extractor = ReasonExtractor()
    reasons = extractor.extract("Иск предъявлен к ненадлежащему ответчику.")
    assert "ненадлежащий ответчик" in reasons
    assert "ненадлежащее исполнение обязанностей" not in reasons


def test_nenadlezhashee_ispolnenie_produces_substantive_label():
    """Bug 3: substantive phrase must produce the correct label."""
    extractor = ReasonExtractor()
    reasons = extractor.extract("Установлено ненадлежащее исполнение обязанностей арбитражным управляющим.")
    assert "ненадлежащее исполнение обязанностей" in reasons


def test_osvedomlennost_ne_dokazana_feminine():
    """Bug 5: feminine form 'не доказана' must be extracted."""
    extractor = ReasonExtractor()
    reasons = extractor.extract("Осведомлённость контрагента о признаках банкротства не доказана.")
    assert "недоказанность обстоятельств" in reasons


def test_fakt_ne_dokazan_masculine():
    """Bug 5: masculine form 'не доказан' must be extracted."""
    extractor = ReasonExtractor()
    reasons = extractor.extract("Факт злоупотребления правом не доказан конкурсным управляющим.")
    assert "недоказанность обстоятельств" in reasons


# ── Reason cap tests ──────────────────────────────────────────────────────────

def test_primary_patterns_capped_at_3():
    """Primary pattern matches should be capped at 3."""
    extractor = ReasonExtractor()
    text = (
        "Неравноценное встречное исполнение. Причинение вреда кредиторам. "
        "Аффилированность сторон. Злоупотребление правом. Мнимость сделки. "
        "Ничтожность сделки."
    )
    reasons = extractor.extract(text)
    assert len(reasons) <= 3


def test_cap_preserves_order():
    """First 3 matched patterns should be returned in order."""
    extractor = ReasonExtractor()
    text = "Неравноценное встречное исполнение и причинение вреда кредиторам при подозрительной сделке и злоупотреблении."
    reasons = extractor.extract(text)
    assert len(reasons) == 3
    assert any("неравноценн" in r for r in reasons)


# ── extract_with_confidence tests ─────────────────────────────────────────────

def test_extract_with_confidence_primary():
    """Primary patterns yield confidence 0.5."""
    extractor = ReasonExtractor()
    reasons, conf = extractor.extract_with_confidence("Злоупотребление правом (ст.10 ГК)")
    assert any("злоупотреб" in r for r in reasons)
    assert conf == 0.5


def test_extract_with_confidence_fallback():
    """Fallback keywords yield confidence 0.3."""
    extractor = ReasonExtractor()
    reasons, conf = extractor.extract_with_confidence("Обстоятельства банкротства")
    assert len(reasons) > 0
    assert conf == 0.3


def test_extract_with_confidence_generic():
    """Generic fallback yields confidence 0.1."""
    extractor = ReasonExtractor()
    reasons, conf = extractor.extract_with_confidence("Служебная заметка.")
    assert reasons == ("оценка обстоятельств дела",)
    assert conf == 0.1


def test_extract_with_confidence_empty():
    """Empty text yields generic fallback with confidence 0.1."""
    extractor = ReasonExtractor()
    reasons, conf = extractor.extract_with_confidence("")
    assert reasons == ("оценка обстоятельств дела",)
    assert conf == 0.1
