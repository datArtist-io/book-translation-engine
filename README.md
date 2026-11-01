# Book Translation Engine

An accuracy-first translation engine for printing presses. Give it a Word document (or a whole folder of them) and it returns **the same .docx, with the same formatting and design, in any language**, with every passage verified and every remaining doubt sent to a human instead of into print.

This is the first build slice of the architecture in *Book-Scale Translation Engine: Full Architecture*: a robust DOCX end-to-end engine with a simple web UI and a CLI.

## What it does

```
.docx / folder ─► ingest (safe parse, tagged text) ─► Book Bible (human approves)
   ─► translate in chunks with memory ─► verify ─► repair ladder ─► book-level audit
   ─► human review (full or statistical sample) ─► export same .docx ─► fidelity gate ─► QA report
```

- **Same file, same design.** Every paragraph becomes *tagged text* (`The <g1>red</g1> house<x2/>`). Tags carry bold, italic, colour, fonts, links, footnote markers, tabs, fields and images through translation and move with the words when word order changes. Only the words of each paragraph change; styles, numbering, tables, headers, footers, footnotes, images and every other part of the file stay byte-identical.
- **Every language.** Any of ~7,900 ISO 639 languages can be chosen. Each gets an honest support tier: high-resource languages are released on a statistical sample; Hausa, Yoruba and Igbo get full native-editor review at first; languages without reliable machine support (Pidgin, minority languages, anything unmeasured) are always delivered as **Draft** until a native editor approves every chunk.
- **Accuracy as a property of every stage**, not a final check:

| Layer | What it catches |
| --- | --- |
| L0 hard gates | missing or broken formatting tags, changed numbers, lost links, glossary violations, wrong script, untranslated text, abnormal length, repetition loops, missing Yoruba/Igbo/Hausa diacritics |
| L1 translation-or-answer | prompt injection (the model answering the book instead of translating it), refusals |
| L2 coverage alignment | omitted or added sentences (Gale-Church alignment, plus a check for merged sentences that hide omissions) |
| L5 independent judge + fact check | facts (numbers, negations, conditions, names) extracted from the source, then confirmed present in the translation by a model from a **different family** |
| L6 book-level audit | names and terms rendered inconsistently across the book |
| L7 human gate | native-editor review; statistical release (exact Clopper-Pearson bound: 299 clean sentences prove < 1% major errors at 95% confidence) |

- **Repair ladder:** draft → targeted fix → resample → different model family → Google NMT → human, with the full evidence attached.
- **Error sweep:** an editor's correction becomes a glossary rule and every affected passage in the book is re-checked.
- **Robustness:** every model call is cached under an idempotency key, so a crash or restart resumes without re-billing; retries with backoff and circuit breakers; per-job token budgets; one failing chunk never stops a book; the export is blocked unless the fidelity gate passes (text equals the approved translation, formatting inventory matches, untouched paragraphs identical, file renders in LibreOffice).

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env        # add your API keys
pytest                      # 43 tests, all offline
bte serve                   # http://127.0.0.1:8000
```

Try everything offline first with the deterministic mock engine:

```bash
BTE_DRAFTER=mock:a BTE_JUDGE=mock:b BTE_ALTERNATE=mock:c BTE_NMT=mock bte serve
```

### API keys and models (`.env`)

| Setting | Default | Role |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | | Claude, the drafter |
| `GEMINI_API_KEY` | | Gemini, the independent judge and alternate drafter |
| `GOOGLE_TRANSLATE_API_KEY` | | Google Cloud Translation (v2), the independent NMT engine |
| `BTE_DRAFTER` | `anthropic:claude-opus-5-5` | `provider:model` |
| `BTE_JUDGE` | `gemini:gemini-2.5-pro` | must be a different family from the drafter |
| `BTE_ALTERNATE` | `gemini:gemini-2.5-pro` | repair-ladder fallback |

Check the exact model ids in each provider's console; models are pinned per book and recorded in the QA report.

### Translate a whole folder

```bash
bte folder ./manuscripts ./translations --source en --target yo --approve-bible
```

Originals are never modified. Output mirrors the folder tree under `translations/yo/`, and `_manifest.csv` accounts for every file (Verified, Draft, Review needed, Rejected with a reason). Re-running resumes and skips files already done. Without `--approve-bible` the Book Bible waits for approval in the web UI, which is the recommended path; add `--draft` to export before human review is complete.

### Web UI

1. Upload a .docx and choose the languages.
2. Review and approve the **Book Bible** (glossary, names, genders, style). One fix here applies to the whole book.
3. Translation runs in the background with live progress.
4. Review flagged and sampled chunks side by side, with formatting visible, findings, the translator's questions and a second opinion.
5. Download the verified .docx and the QA report.

Set `BTE_ACCESS_TOKEN` before serving beyond localhost (the server refuses otherwise).

## Security

- DOCX parsing rejects encrypted/legacy files, macros, zip bombs, DOCTYPE and entity declarations; XML is parsed with entities, DTDs and network access disabled.
- Book text always travels as quoted JSON data with an explicit instruction that it is never to be obeyed; models get no tools; outputs are validated against strict schemas.
- Web: access-token login with rate limiting, SameSite=Strict HttpOnly cookies, CSRF tokens on every form, same-origin checks, strict CSP and security headers, no API docs exposed, upload size limits.
- API keys travel in headers (never URLs); manuscript text is never logged.
- `bandit` static analysis: no medium or high findings.

## Code map

| Path | Architecture section |
| --- | --- |
| `bte/docx/` | §3 ingest, §8 reassembly, formatting fidelity |
| `bte/languages.py` | §9 Language Packs and tiers |
| `bte/pipeline/prompts.py` | §6 translation contract, §7 judge, §4 Book Bible |
| `bte/pipeline/engine.py` | §6–7 repair ladder, refinement, risk score |
| `bte/pipeline/gates.py`, `bte/text/align.py` | §7 L0–L2 |
| `bte/pipeline/audit.py` | §7 L6 |
| `bte/pipeline/release.py` | §7 statistical release |
| `bte/pipeline/gateway.py` | §11 retries, caching, budgets |
| `bte/service.py`, `bte/store.py`, `bte/worker.py` | §10–11 durable workflow and data model |
| `bte/web/`, `bte/cli.py` | §13 product, folder intake |

## Built in this slice vs next

Built: everything above, for DOCX, in any language pair.

Next, in order:
1. **Phase 0 benchmark** with real keys: gold sets for English to Yoruba, Hausa and Igbo to calibrate thresholds and confirm engine routing.
2. Learned quality estimation (SSA-COMET-QE for African languages, CometKiwi or MetricX for others) as layer L4.
3. Word-level alignment for the full Word Ledger, and multi-candidate ensemble selection for flagged chunks.
4. Batch APIs and prompt caching tuned for cost; Postgres deployment.
5. More formats: EPUB, IDML/INDD via InDesign Server, PDF rebuild, scanned books with OCR.
6. Desktop companion app with hot folders.
