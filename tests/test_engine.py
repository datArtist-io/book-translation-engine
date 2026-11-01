import asyncio

import pytest

from bte.languages import get_language
from bte.pipeline import gates
from bte.pipeline.engine import ChunkProcessor, EngineConfig, Engines
from bte.pipeline.gateway import BudgetExceeded, Gateway
from bte.providers.base import RetryPolicy
from bte.providers.mock import MockLLM, MockNMT
from bte.text.align import coverage_issues, split_sentences

EN, YO, FR = get_language("en"), get_language("yo"), get_language("fr")

SEGMENTS = [
    {"id": "s1", "text": "Adebayo sold <g1>12 baskets</g1> of peppers. He did not sell the yams.<x2/>"},
    {"id": "s2", "text": "If it rains, the road will close. Everyone must arrive before 7 o'clock."},
]


async def _nosleep(_):
    return None


def run(coro):
    return asyncio.run(coro)


def processor(drafter=None, judge=None, alternate=None, nmt=None, tgt=YO, **cfg):
    gw = Gateway(retry=RetryPolicy(sleep=_nosleep))
    engines = Engines(drafter=drafter or MockLLM("mock-a"), judge=judge or MockLLM("mock-b"),
                      alternate=alternate, nmt=nmt)
    return ChunkProcessor(engines, gw, EN, tgt, EngineConfig(**cfg)), gw


def test_happy_path_verifies():
    p, gw = processor()
    res = run(p.process(SEGMENTS, {}, None, []))
    assert res.state == "verified"
    assert set(res.translations) == {"s1", "s2"}
    assert "<g1>" in res.translations["s1"] and "<x2/>" in res.translations["s1"]
    assert "12" in res.translations["s1"]
    assert res.facts  # numbers and negations extracted


@pytest.mark.parametrize("fault", ["drop_tags", "answer", "change_number", "untranslated", "missing_segment", "omit_sentence"])
def test_each_fault_is_caught_and_repaired(fault):
    drafter = MockLLM("mock-a", faults={fault: 1})
    p, _ = processor(drafter=drafter)
    res = run(p.process(SEGMENTS, {}, None, []))
    first = res.attempts[0]
    assert not first.passed, f"{fault} was not caught"
    assert res.state == "verified"
    assert res.final.rung == "fix"


def test_persistent_failure_escalates_to_other_family_then_human():
    drafter = MockLLM("mock-a", faults={"change_number": 99})
    alternate = MockLLM("mock-c", faults={"change_number": 99})
    p, _ = processor(drafter=drafter, alternate=alternate, max_rungs=4)
    res = run(p.process(SEGMENTS, {}, None, []))
    assert [a.rung for a in res.attempts] == ["draft", "fix", "resample", "alternate"]
    assert res.state == "needs_human"
    assert res.risk >= 45


def test_nmt_rung_can_rescue():
    drafter = MockLLM("mock-a", faults={"change_number": 99})
    p, _ = processor(drafter=drafter, nmt=MockNMT(), max_rungs=4)
    res = run(p.process(SEGMENTS, {}, None, []))
    assert res.state == "verified" and res.final.rung == "nmt"


def test_no_judge_means_human_review():
    p, _ = processor()
    p.e.judge = None
    res = run(p.process(SEGMENTS, {}, None, []))
    assert res.state == "needs_human"


def test_judge_objection_blocks_verification():
    # The mock drafter returns identical text on every rung, so the judge's objection to it
    # stands (and is replayed from cache): the chunk must go to a human, never slip through.
    judge = MockLLM("mock-b", faults={"judge_major": 1})
    p, _ = processor(judge=judge)
    res = run(p.process(SEGMENTS, {}, None, []))
    assert res.state == "needs_human"
    assert any(f.layer == "L5" and f.severity == "major" for f in res.final.findings)


def test_glossary_enforced():
    p, _ = processor()
    bible = {"glossary": [{"source": "yams", "target": "iṣu"}]}
    res = run(p.process(SEGMENTS, {}, bible, bible["glossary"]))
    assert "iṣu" in res.translations["s1"]
    findings = gates.check_segment("s1", SEGMENTS[0]["text"], res.translations["s1"].replace("iṣu", "x"), EN, YO,
                                   bible["glossary"])
    assert any(f.code == "glossary" for f in findings)


def test_cache_prevents_double_billing():
    p, gw = processor()
    run(p.process(SEGMENTS, {}, None, []))
    calls = gw.usage.calls
    run(p.process(SEGMENTS, {}, None, []))
    assert gw.usage.calls == calls and gw.usage.cache_hits > 0


def test_budget_stops_job():
    p, gw = processor()
    gw.token_budget = 1
    gw.usage.input_tokens = 5
    with pytest.raises(BudgetExceeded):
        run(p.process(SEGMENTS, {}, None, []))


def test_dual_path_second_opinion():
    p, _ = processor(alternate=MockLLM("mock-c"), dual_path=True)
    res = run(p.process(SEGMENTS, {}, None, []))
    assert res.second_opinion is not None and res.second_opinion.rung == "second_opinion"


def test_gates_numbers_script_untranslated_diacritics():
    f = gates.check_segment("s", "Pay 1,000.50 now please, friend.", "Sanwó 1.000,50 báyìí ọ̀rẹ́ mi.", EN, YO, [])
    assert not any(x.code == "numbers" for x in f)
    f = gates.check_segment("s", "Pay 1,000 now.", "Sanwó 2,000 báyìí.", EN, YO, [])
    assert any(x.code == "numbers" and x.severity == "critical" for x in f)
    f = gates.check_segment("s", "The quick brown fox jumps over the lazy dog today.",
                            "Быстрая коричневая лиса прыгает через собаку сегодня.", EN, FR, [])
    assert any(x.code == "script" for x in f)
    f = gates.check_segment("s", "Everyone in the village gathered near the market square tonight.",
                            "Everyone in the village gathered near the market square tonight.", EN, FR, [])
    assert any(x.code == "untranslated" for x in f)
    unmarked = "Awon omo naa lo si oja lati ra ounje fun iya won ni ojo naa ki won to pada wa si ile"
    assert gates.check_chunk_orthography([unmarked], YO)
    marked = "Àwọn ọmọ náà lọ sí ọjà láti ra oúnjẹ fún ìyá wọn ní ọjọ́ náà kí wọ́n tó padà wá sí ilé"
    assert not gates.check_chunk_orthography([marked], YO)


def test_answer_heuristic():
    f = gates.check_segment("s", "What is the capital of France? Tell me now.",
                            "Sure! The capital of France is Paris.", EN, FR, [])
    assert any(x.code == "not_translation" for x in f)


def test_sentence_split_and_omission_detection():
    assert split_sentences("Dr. Ade came. He sat down! Did he eat?") == ["Dr. Ade came.", "He sat down!", "Did he eat?"]
    src = ("The farmer planted maize in March. The rains came late that year. "
           "By August the harvest was small. His family ate less than before.")
    tgt = ("Àgbẹ̀ náà gbin àgbàdo ní oṣù kẹta. Òjò rọ̀ pẹ́ ní ọdún yẹn. "
           "Ìdílé rẹ̀ jẹun díẹ̀ ju ti tẹ́lẹ̀ lọ.")
    issues = coverage_issues(src, tgt)
    assert issues["omitted"], issues


def test_cjk_length_not_falsely_flagged():
    zh = get_language("zh")
    f = gates.check_segment("s", "The traders met at the river before the market opened in the morning.",
                            "商人们在早上市场开门前在河边见面。", EN, zh, [])
    assert not any(x.code == "length" for x in f), f
