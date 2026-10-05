"""Knowledge Graph (Neo4j) + GraphRAG over two drug-topic knowledge bases.

Contract (fixed — bench_kg.py and the tests rely on it):
    link_entity(name, known)                       -> one of `known` or None          (TODO KG-1)
    build_graph(graph, law_docs, news_docs, llm_fn)   load both KBs into Neo4j      (TODO KG-2)
        every node created from ONE document carries the property `doc_id`
    Neo4jGraph.context(question, doc_ids)         -> list[str] facts               (TODO KG-3)
    GraphRAGAgent.answer(question, top_k)         -> str                           (TODO KG-4)

Everything else in this file is a HINT: one possible ontology (below). Use it as is, change it,
or design your own — your own ontology + report/ONTOLOGY.md earns the bonus (see SUBMISSION.md).

Suggested ontology (Crime is the bridge between the law KB and the news KB):

    (:Article {id, title, law, doc_id})-[:DEFINES]->(:Crime {name})
    (:Article)-[:HAS_CLAUSE]->(:Clause {id, number, penalty, text})-[:MENTIONS]->(:Substance {name})
    (:Case {name, summary, date, doc_id})-[:CHARGED_WITH]->(:Crime)
    (:Case)-[:INVOLVES {amount}]->(:Substance)
    (:Case)-[:LOCATED_IN]->(:Location {name})
    (:Person {name, aliases})-[:INVOLVED_IN {role, sentence, charge}]->(:Case)
"""

from __future__ import annotations

import difflib
import json
import os
import re
import unicodedata
from pathlib import Path
from typing import Any, Callable

from .models import Document
from .store import EmbeddingStore

# Canonical substance names: the ones BLHS Chương XX lists, plus common ones in Vietnamese news.
SUBSTANCES = ["Heroine", "Cocaine", "Methamphetamine", "Amphetamine", "MDMA", "XLR-11", "Ketamine",
              "cần sa", "thuốc phiện", "côca"]
CLAUSE_START = re.compile(r"^(\d+)\.\s", re.MULTILINE)
FOOTNOTE = re.compile(r"\[\d+\]")

def load_markdown_docs(folder: str | Path) -> list[Document]:
    """Read crawler output (.md with a flat `key: "value"` front matter) into Documents."""
    docs = []
    for path in sorted(Path(folder).glob("*.md")):
        raw = path.read_text(encoding="utf-8")
        _, front, body = raw.split("---", 2)
        metadata = {k: json.loads(v) for k, v in re.findall(r'^(\w+): (".*")$', front, re.MULTILINE)}
        docs.append(Document(id=metadata.get("doc_id", path.stem), content=body.strip(), metadata=metadata))
    return docs

def normalize_crime(name: str) -> str:
    """'Tội Mua bán trái phép chất ma túy' -> 'mua bán trái phép chất ma túy'."""
    name = re.sub(r"\s+", " ", name.strip().strip("\"'“”").lower())
    return name.removeprefix("tội ").strip()

def link_entity(name: str, known: list[str], normalize: Callable[[str], str] = normalize_crime) -> str | None:
    """Map a free-text mention (e.g. a charge written by a journalist) onto one canonical name in `known`."""
    target = normalize(name or "")
    if not target:
        return None
    by_norm: dict[str, str] = {}
    for item in known:                     # first spelling wins if two known names normalize the same
        by_norm.setdefault(normalize(item), item)
    if target in by_norm:
        return by_norm[target]
    for close in difflib.get_close_matches(target, list(by_norm), n=3, cutoff=0.8):
        if _same_words(target, close):
            return by_norm[close]
    return None

_FILLER_WORDS = {"tội", "hành", "vi", "việc", "các", "của", "và", "hoặc"}

def _strip_accents(text: str) -> str:
    text = unicodedata.normalize("NFD", text.replace("đ", "d").replace("Đ", "D"))
    return "".join(ch for ch in text if unicodedata.category(ch) != "Mn")

def _same_words(a: str, b: str) -> bool:
    """Character ratio alone links 'sử dụng trái phép chất ma túy' to 'TỔ CHỨC sử dụng…' (0.88).
    Require every content word on each side to have a near-identical word (accents ignored) on the other."""
    wa, wb = ([w for w in re.findall(r"\w+", _strip_accents(s)) if w not in {_strip_accents(f) for f in _FILLER_WORDS}]
              for s in (a, b))
    def covered(xs: list[str], ys: list[str]) -> bool:
        return all(any(difflib.SequenceMatcher(None, x, y).ratio() >= 0.8 for y in ys) for x in xs)
    return covered(wa, wb) and covered(wb, wa)

def find_substances(text: str) -> list[str]:
    lowered = text.lower()
    return [name for name in SUBSTANCES if name.lower() in lowered]

# ----------------------------------------------------------------------------------------------
# HINT — suggested ontology: extraction helpers
# ----------------------------------------------------------------------------------------------

def parse_law_article(doc: Document) -> dict[str, Any]:
    """Deterministic (regex) extraction for one 'Điều' — law text is regular enough to skip the LLM."""
    article_id = doc.metadata["article"]                       # "Điều 251 BLHS"
    title = doc.metadata["title"].split(". ", 1)[-1]           # "Tội mua bán trái phép chất ma túy"
    body = FOOTNOTE.sub("", doc.content)
    starts = list(CLAUSE_START.finditer(body))
    clauses = []
    for index, start in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(body)
        text = body[start.start():end].strip()
        first_line = text.splitlines()[0]
        penalty = re.search(r"\bbị ((?:phạt|tù|cảnh cáo).+?)(?::|$)", first_line)
        clauses.append({
            "id": f"{article_id} khoản {start.group(1)}",
            "number": int(start.group(1)),
            "penalty": penalty.group(1).rstrip(".") if penalty else "",
            "text": text,
            "substances": find_substances(text),
        })
    return {
        "id": article_id,
        "law": doc.metadata.get("law", ""),
        "title": title,
        "doc_id": doc.id,
        "crime": normalize_crime(title) if title.startswith("Tội ") else None,
        "clauses": clauses,
    }

NEWS_EXTRACTION_PROMPT = """Bạn trích xuất knowledge graph từ một bài báo tiếng Việt về ma túy.
Chỉ dùng thông tin có trong bài. Trả về JSON đúng dạng:
{{"cases": [{{
  "name": "tên ngắn của vụ việc, ví dụ: Vụ mua bán 36kg ma túy tại TP.HCM",
  "summary": "1-2 câu tóm tắt",
  "date": "ngày xảy ra/xét xử nếu có, dạng YYYY-MM-DD hoặc chuỗi rỗng",
  "location": "tỉnh/thành phố, chuỗi rỗng nếu không rõ",
  "charges": ["tội danh, BẮT BUỘC chọn đúng nguyên văn từ DANH SÁCH TỘI DANH"],
  "substances": [{{"name": "tên chất, dùng tên chuẩn trong DANH SÁCH CHẤT nếu khớp", "amount": "khối lượng nếu có"}}],
  "people": [{{"name": "họ tên", "aliases": ["biệt danh"], "role": "bị cáo|bị can|nghi phạm|người liên quan|cán bộ",
               "charge": "tội danh của người này (từ DANH SÁCH TỘI DANH) hoặc chuỗi rỗng",
               "sentence": "mức án nếu có, ví dụ: tử hình, 8 năm tù"}}]
}}]}}
Bài không nói về vụ việc cụ thể (tuyên truyền, hội nghị...) thì trả về {{"cases": []}}.

DANH SÁCH TỘI DANH: {crimes}
DANH SÁCH CHẤT: {substances}

Tiêu đề: {title}
Nội dung:
{content}"""

def extract_news_cases(doc: Document, llm_fn: Callable[[str], str], known_crimes: list[str]) -> list[dict]:
    """LLM extraction for one news article; charges are re-linked to law-KB crimes in code."""
    prompt = NEWS_EXTRACTION_PROMPT.format(
        crimes="; ".join(known_crimes), substances=", ".join(SUBSTANCES),
        title=doc.metadata.get("title", ""), content=doc.content[:12000],
    )
    try:
        cases = json.loads(llm_fn(prompt)).get("cases", [])
    except (json.JSONDecodeError, AttributeError):
        return []
    for case in cases:
        case["charges"] = sorted({c for c in (link_entity(x, known_crimes) for x in case.get("charges", [])) if c})
        for person in case.get("people", []):
            person["charge"] = link_entity(person.get("charge") or "", known_crimes) or ""
    return cases

# ----------------------------------------------------------------------------------------------
# OWN ontology (report/ONTOLOGY.md): extraction helpers. Select with KG_ONTOLOGY=own (default) | hint.
# ----------------------------------------------------------------------------------------------

def ontology_mode() -> str:
    return os.getenv("KG_ONTOLOGY", "own").strip().lower()

# Substance groups exactly as BLHS points name them -> canonical Substance names (threshold targets).
LAW_SUBSTANCE_GROUPS = [
    (re.compile(r"^Heroine, Cocaine"), ["Heroine", "Cocaine", "Methamphetamine", "Amphetamine", "MDMA", "XLR-11"]),
    (re.compile(r"^Nhựa thuốc phiện"), ["nhựa thuốc phiện", "nhựa cần sa", "cao côca"]),
    (re.compile(r"^Lá cây côca"), ["lá côca", "lá khát", "cần sa"]),
    (re.compile(r"^Quả thuốc phiện khô"), ["quả thuốc phiện khô"]),
    (re.compile(r"^Quả thuốc phiện tươi"), ["quả thuốc phiện tươi"]),
    (re.compile(r"^Các chất ma túy khác ở thể rắn"), ["chất ma túy khác (rắn)"]),
    (re.compile(r"^Các chất ma túy khác ở thể lỏng"), ["chất ma túy khác (lỏng)"]),
    (re.compile(r"^Tiền chất ở thể rắn"), ["tiền chất (rắn)"]),
    (re.compile(r"^Tiền chất ở thể lỏng"), ["tiền chất (lỏng)"]),
]
# News spelling / street names -> canonical name. Lower-case keys.
SUBSTANCE_ALIASES = {
    "MDMA": ["thuốc lắc", "ecstasy", "kẹo", "ma túy kẹo", "viên nén mdma"],
    "Methamphetamine": ["ma túy đá", "methamphetamin", "meth", "hồng phiến", "ma túy hồng phiến"],
    "Amphetamine": ["amphetamin"],
    "Heroine": ["heroin", "hêrôin", "hê rô in"],
    "Cocaine": ["cocain", "côcain", "coke"],
    "Ketamine": ["ketamin", "ke"],
    "Etomidate": ["etomidat", "pod chill", "ma túy pod chill"],
    "cần sa": ["cỏ", "marijuana", "cần sa khô", "hoa cần sa", "lá cần sa"],
    "nhựa thuốc phiện": ["thuốc phiện"],
}
# Drugs the law does not name: thresholds come from "các chất ma túy khác" (solid / liquid).
OTHER_DRUG_STATE = {"Ketamine": "rắn", "Etomidate": "lỏng"}
GENERIC_SUBSTANCES = {"ma túy", "ma tuý", "chất ma túy", "ma túy các loại", "các loại ma túy"}

NUM = r"(\d+(?:[.,]\d+)*)"
UNIT = r"(kilôgam|kilogam|kilogram|kg|gam|gram|g|mililít|mililit|ml|lít|lit|l)(?![\wà-ỹ])"
THRESHOLD_RANGE = re.compile(rf"(?:từ|tù)\s+{NUM}\s*{UNIT}\s+đến dưới\s+{NUM}\s*{UNIT}")
THRESHOLD_OPEN = re.compile(rf"{NUM}\s*{UNIT}\s+trở lên")
POINT = re.compile(r"^([a-zđ])\)\s+(.+)$", re.MULTILINE)

def _number(text: str) -> float:
    """Vietnamese numbers: '.' groups thousands, ',' is the decimal mark ('1.000' -> 1000, '0,1' -> 0.1)."""
    return float(text.replace(".", "").replace(",", "."))

def to_base_unit(value: float, unit: str) -> tuple[float, str]:
    """-> (amount in gram or millilitre, 'mass' | 'volume')."""
    unit = unit.lower()
    if unit in {"kilôgam", "kilogam", "kilogram", "kg"}:
        return value * 1000, "mass"
    if unit in {"gam", "gram", "g"}:
        return value, "mass"
    if unit in {"lít", "lit", "l"}:
        return value * 1000, "volume"
    return value, "volume"

def parse_amount(text: str) -> tuple[float | None, str | None]:
    """'hơn 9,6kg' -> (9600.0, 'mass'); '5 viên' -> (None, None): pills cannot be compared to a gram threshold."""
    match = re.search(rf"{NUM}\s*{UNIT}", text or "", re.IGNORECASE)
    if not match:
        return None, None
    return to_base_unit(_number(match.group(1)), match.group(2))

def parse_thresholds(clause_text: str) -> list[dict]:
    """Every point 'X có khối lượng từ A đến dưới B' -> {point, substance, min_g, max_g, measure}."""
    out = []
    for letter, text in POINT.findall(clause_text):
        names = next((names for pattern, names in LAW_SUBSTANCE_GROUPS if pattern.search(text)), None)
        if not names:
            continue
        if m := THRESHOLD_RANGE.search(text):
            low, measure = to_base_unit(_number(m.group(1)), m.group(2))
            high, _ = to_base_unit(_number(m.group(3)), m.group(4))
        elif m := THRESHOLD_OPEN.search(text):
            (low, measure), high = to_base_unit(_number(m.group(1)), m.group(2)), None
        else:
            continue
        out += [{"point": letter, "substance": n, "min_g": low, "max_g": high, "measure": measure} for n in names]
    return out

def penalty_scale(penalty: str) -> tuple[float | None, float | None, float]:
    """'phạt tù từ 07 năm đến 15 năm' -> (7, 15, 15); death > life > years; fines/side penalties score 0."""
    if not re.search(r"\btù\b|tử hình", penalty):
        return None, None, 0.0
    years = [int(n) / (12 if unit == "tháng" else 1) for n, unit in re.findall(r"(\d+)\s*(năm|tháng)", penalty)]
    low, high = (min(years), max(years)) if years else (None, None)
    severity = 100.0 if "tử hình" in penalty else 50.0 if "chung thân" in penalty else (high or 0.0)
    return low, high, severity

def parse_terms(article: dict) -> list[dict]:
    """Definition clauses 'N. <Term> là …' (Luật PCMT Điều 2) -> [{name, clause_id, definition}]."""
    terms = []
    for clause in article["clauses"]:
        flat = re.sub(r"\s+", " ", clause["text"])
        if m := re.match(r"^\d+\.\s+(.{3,80}?)\s+là\s", flat):
            terms.append({"name": m.group(1).lower(), "clause_id": clause["id"], "definition": flat})
    return terms

def parse_law_article_own(doc: Document) -> dict[str, Any]:
    article = parse_law_article(doc)
    for clause in article["clauses"]:
        clause["min_years"], clause["max_years"], clause["severity"] = penalty_scale(clause["penalty"])
        clause["thresholds"] = parse_thresholds(clause["text"])
    article["terms"] = parse_terms(article) if "Giải thích từ ngữ" in article["title"] else []
    return article

def canonical_substance(name: str) -> str | None:
    """Map a news mention to a canonical substance name; None for generic words ('ma túy')."""
    key = re.sub(r"\s+", " ", (name or "").strip().strip("\"'“”").lower())
    if not key or key in GENERIC_SUBSTANCES:
        return None
    for canon, aliases in SUBSTANCE_ALIASES.items():
        if key == canon.lower() or key in aliases:
            return canon
    known = [n for _, names in LAW_SUBSTANCE_GROUPS for n in names] + list(OTHER_DRUG_STATE)
    return link_entity(key, known, normalize=lambda s: s.strip().lower()) or key

def normalize_person(name: str) -> str:
    name = unicodedata.normalize("NFC", re.sub(r"\s+", " ", (name or "").strip().strip("\"'“”")))
    name = re.sub(r"^(bị cáo|bị can|nghi phạm|đối tượng|ông|bà|anh|chị)\s+", "", name, flags=re.IGNORECASE)
    return name.lower()

LOCATION_ALIASES = {"TP.HCM": ["tp.hcm", "tphcm", "tp hcm", "tp hồ chí minh", "thành phố hồ chí minh", "hồ chí minh", "sài gòn"],
                    "Hà Nội": ["hà nội", "tp hà nội", "thành phố hà nội", "tp. hà nội"]}

def normalize_location(name: str) -> str:
    key = re.sub(r"\s+", " ", (name or "").strip()).lower()
    for canon, aliases in LOCATION_ALIASES.items():
        if key in aliases:
            return canon
    return re.sub(r"^(tỉnh|thành phố|tp\.?)\s+", "", (name or "").strip(), flags=re.IGNORECASE)

STAGES = ["bắt giữ", "khởi tố", "truy tố", "xét xử sơ thẩm", "xét xử phúc thẩm", "khác"]
ACCUSED_ROLES = {"bị cáo", "bị can", "nghi phạm"}

def split_related_teaser(content: str) -> tuple[str, str]:
    """The crawler glued the 'related article' sapo onto 17/20 news bodies as the LAST paragraph
    (report/ONTOLOGY.md O7). Keep it out of extraction so its people/cases are not attributed to this article."""
    paragraphs = [p for p in content.split("\n\n") if p.strip()]
    if len(paragraphs) < 4:
        return content, ""
    return "\n\n".join(paragraphs[:-1]), paragraphs[-1]

OWN_EXTRACTION_PROMPT = """Bạn trích xuất knowledge graph từ một bài báo tiếng Việt về ma túy. Chỉ dùng thông tin có trong bài.
Trả về JSON đúng dạng:
{{"cases": [{{
  "name": "tên ngắn của vụ việc, ví dụ: Vụ Cái Quang Huy vận chuyển 9,6kg MDMA từ Đức",
  "summary": "1-2 câu tóm tắt",
  "date": "ngày xảy ra/xét xử nếu có (YYYY-MM-DD) hoặc chuỗi rỗng",
  "location": "tỉnh/thành phố, chuỗi rỗng nếu không rõ",
  "stage": "giai đoạn tố tụng mà BÀI BÁO NÀY đưa tin, một trong: {stages}",
  "charges": ["tội danh của vụ, mỗi phần tử MỘT tội, chọn nguyên văn từ DANH SÁCH TỘI DANH"],
  "substances": [{{"name": "tên chất, dùng tên chuẩn trong DANH SÁCH CHẤT nếu khớp", "amount": "khối lượng kèm đơn vị, nguyên văn, ví dụ 'hơn 9,6kg'"}}],
  "people": [{{
    "name": "họ tên đầy đủ (không kèm chức danh)",
    "aliases": ["biệt danh, tên tài khoản mạng xã hội"],
    "role": "bị cáo | bị can | nghi phạm | người liên quan",
    "charges": [{{"crime": "MỘT tội, nguyên văn từ DANH SÁCH TỘI DANH; nếu tội không có trong danh sách thì ghi nguyên văn tội trong bài",
                  "stage": "giai đoạn của việc buộc tội này, một trong: {stages}",
                  "sentence": "mức án tòa đã tuyên (ví dụ: 36 tháng tù, tử hình); chuỗi rỗng nếu chưa xét xử"}}]
  }}]
}}]}}
Quy tắc:
- Không đưa cán bộ công an, thẩm phán, luật sư, nhà báo vào "people" trừ khi chính họ bị buộc tội.
- Một bài có thể nói về nhiều vụ khác nhau; mỗi vụ một phần tử. Bài không nói về vụ việc cụ thể thì trả về {{"cases": []}}.
- "bị bắt về hành vi X" là buộc tội X ở giai đoạn "bắt giữ". Hành vi "sử dụng trái phép chất ma túy" KHÔNG phải tội trong danh sách.
- Mức án của phiên sơ thẩm được nhắc lại trong bài phúc thẩm thì stage là "xét xử sơ thẩm".

DANH SÁCH TỘI DANH: {crimes}
DANH SÁCH CHẤT: {substances}

Tiêu đề: {title}
Nội dung:
{content}"""

def _parse_json(text: str) -> dict:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text or "", re.DOTALL)   # some providers wrap JSON in prose
        try:
            return json.loads(match.group(0)) if match else {}
        except json.JSONDecodeError:
            return {}

def extract_news_own(doc: Document, llm_fn: Callable[[str], str], known_crimes: list[str]) -> list[dict]:
    """LLM -> JSON, then deterministic clean-up: crimes via link_entity, substances via the synonym table."""
    body, _teaser = split_related_teaser(doc.content)
    prompt = OWN_EXTRACTION_PROMPT.format(
        stages=" | ".join(STAGES), crimes="; ".join(known_crimes),
        substances=", ".join(list(SUBSTANCE_ALIASES) + ["Amphetamine", "XLR-11"]),
        title=doc.metadata.get("title", ""), content=body[:12000],
    )
    data = _parse_json(llm_fn(prompt))
    cases = data.get("cases", []) if isinstance(data, dict) else []
    for case in cases:
        case["stage"] = case.get("stage") if case.get("stage") in STAGES else "khác"
        case["charges"] = sorted({c for c in (link_entity(_strip_behaviour(x), known_crimes) for x in case.get("charges") or []) if c})
        substances = {}
        for s in case.get("substances") or []:
            canon = canonical_substance(s.get("name", ""))
            if not canon:
                continue
            grams, measure = parse_amount(s.get("amount", ""))
            old = substances.get(canon)
            if not old or (grams or 0) > (old["amount_g"] or 0):   # the same drug listed twice: keep the total
                substances[canon] = {"name": canon, "amount": s.get("amount", ""), "amount_g": grams, "measure": measure}
        case["substances"] = list(substances.values())
        people = []
        for p in case.get("people") or []:
            if not p.get("name"):
                continue
            charges = []
            for ch in p.get("charges") or []:
                raw = (ch.get("crime") or "").strip()
                if not raw:
                    continue
                charges.append({"crime": link_entity(_strip_behaviour(raw), known_crimes), "charge_text": raw,
                                "stage": ch.get("stage") if ch.get("stage") in STAGES else case["stage"],
                                "sentence": (ch.get("sentence") or "").strip()})
            people.append({"name": p["name"].strip(), "aliases": [a for a in p.get("aliases") or [] if a],
                           "role": (p.get("role") or "người liên quan").strip(), "charges": charges})
        case["people"] = people
    return cases

def _strip_behaviour(text: str) -> str:
    """'hành vi tổ chức sử dụng…' -> 'tổ chức sử dụng…' (normalize_crime only drops the 'tội ' prefix)."""
    return re.sub(r"^\s*(về\s+)?(hành vi|tội)\s+", "", text or "", flags=re.IGNORECASE)

class EntityResolver:
    """Deterministic identity for Person and Case across articles (report/ONTOLOGY.md decision 2).

    Person: key = normalized full name; every alias points at the same pid.
    Case:   a newly extracted case joins an existing case if they share at least one accused person.
    """

    def __init__(self) -> None:
        self.pid_by_key: dict[str, str] = {}
        self.cases: list[dict] = []          # {"case_id", "accused": set[pid]}

    def person(self, name: str, aliases: list[str]) -> str:
        keys = [normalize_person(name)] + [normalize_person(a) for a in aliases]
        pid = next((self.pid_by_key[k] for k in keys if k in self.pid_by_key), keys[0])
        for k in keys:
            if k and len(k) >= 3:
                self.pid_by_key.setdefault(k, pid)
        return pid

    def case(self, accused: set[str], fallback: str) -> str:
        for known in self.cases:
            if known["accused"] & accused:
                known["accused"] |= accused
                return known["case_id"]
        case_id = f"case:{sorted(accused)[0]}" if accused else f"case:{fallback}"
        self.cases.append({"case_id": case_id, "accused": set(accused)})
        return case_id

# ----------------------------------------------------------------------------------------------
# Neo4j
# ----------------------------------------------------------------------------------------------

class Neo4jGraph:
    """Thin wrapper over the official neo4j driver."""

    def __init__(self, uri: str, user: str, password: str) -> None:
        from neo4j import GraphDatabase

        # Generous timeouts/retries: a remote instance (Neo4j Aura) over a slow link can take seconds per round trip.
        self.driver = GraphDatabase.driver(uri, auth=(user, password), notifications_min_severity="OFF",
                                           connection_timeout=60, max_transaction_retry_time=180,
                                           connection_acquisition_timeout=180)
        self.driver.verify_connectivity()

    def close(self) -> None:
        self.driver.close()

    def run(self, cypher: str, **params: Any) -> list[dict]:
        records, _, _ = self.driver.execute_query(cypher, params)
        return [record.data() for record in records]

    def reset(self) -> None:
        """Delete every node, relationship and constraint (bench_kg.py calls this before build_graph)."""
        self.run("MATCH (n) DETACH DELETE n")
        for row in self.run("SHOW CONSTRAINTS YIELD name RETURN name"):
            self.run(f"DROP CONSTRAINT `{row['name']}` IF EXISTS")

    def stats(self) -> dict[str, int]:
        nodes = self.run("MATCH (n) RETURN count(n) AS n")[0]["n"]
        rels = self.run("MATCH ()-[r]->() RETURN count(r) AS n")[0]["n"]
        return {"nodes": nodes, "relationships": rels}

    def seed_facts(self, question: str, doc_ids: list[str], skip_labels: tuple[str, ...] = (),
                   limit: int = 60) -> tuple[list[str], list[str]]:
        """Ontology-independent first step: seed nodes + their 1-hop edges as text facts.

        Seeds = nodes whose `doc_id` is in doc_ids, or whose `name`/`aliases` appear in the question.
        Returns (seed elementIds, facts). Nodes with a label in skip_labels are left out of the facts.
        """
        seeds = self.run(
            """
            MATCH (n)
            WHERE n.doc_id IN $doc_ids
               OR (n.name IS :: STRING AND size(n.name) >= 3 AND toLower($q) CONTAINS toLower(n.name))
               OR any(a IN coalesce(n.aliases, []) WHERE size(a) >= 3 AND toLower($q) CONTAINS toLower(a))
            RETURN elementId(n) AS id
            """,
            q=question, doc_ids=doc_ids,
        )
        seed_ids = [row["id"] for row in seeds]
        edges = self.run(
            """
            MATCH (s)-[r]-(m)
            WHERE elementId(s) IN $ids
              AND none(l IN labels(s) + labels(m) WHERE l IN $skip)
            WITH DISTINCT r LIMIT $limit
            WITH startNode(r) AS a, r, endNode(r) AS b
            RETURN labels(a)[0] AS a_label, coalesce(a.name, a.id) AS a_name, type(r) AS rel,
                   properties(r) AS props, labels(b)[0] AS b_label, coalesce(b.name, b.id) AS b_name
            """,
            ids=seed_ids, skip=list(skip_labels), limit=limit,
        )
        facts = []
        for e in edges:
            props = ", ".join(f"{k}: {v}" for k, v in e["props"].items() if v)
            facts.append(f"({e['a_label']}: {e['a_name']}) -[{e['rel']}{' {' + props + '}' if props else ''}]-> "
                         f"({e['b_label']}: {e['b_name']})")
        return seed_ids, facts

    # ---------------------------------------------------------------- HINT — suggested ontology: writes

    def suggested_constraints(self) -> None:
        for label, key in [("Article", "id"), ("Clause", "id"), ("Crime", "name"), ("Case", "name"),
                           ("Substance", "name"), ("Person", "name"), ("Location", "name")]:
            self.run(f"CREATE CONSTRAINT IF NOT EXISTS FOR (n:{label}) REQUIRE n.{key} IS UNIQUE")

    def add_law_article(self, article: dict) -> None:
        self.run(
            """
            MERGE (a:Article {id: $id}) SET a.title = $title, a.law = $law, a.doc_id = $doc_id
            FOREACH (crime IN CASE WHEN $crime IS NULL THEN [] ELSE [$crime] END |
                MERGE (c:Crime {name: crime}) MERGE (a)-[:DEFINES]->(c))
            WITH a
            UNWIND $clauses AS clause
            MERGE (cl:Clause {id: clause.id})
              SET cl.number = clause.number, cl.penalty = clause.penalty, cl.text = clause.text, cl.doc_id = $doc_id
            MERGE (a)-[:HAS_CLAUSE]->(cl)
            FOREACH (s IN clause.substances | MERGE (sub:Substance {name: s}) MERGE (cl)-[:MENTIONS]->(sub))
            """,
            **article,
        )

    def add_news_case(self, case: dict, doc: Document) -> None:
        self.run(
            """
            MERGE (k:Case {name: $name})
              SET k.summary = $summary, k.date = $date, k.doc_id = $doc_id, k.source_title = $title
            FOREACH (loc IN CASE WHEN $location = '' THEN [] ELSE [$location] END |
                MERGE (l:Location {name: loc}) MERGE (k)-[:LOCATED_IN]->(l))
            FOREACH (crime IN $charges | MERGE (c:Crime {name: crime}) MERGE (k)-[:CHARGED_WITH]->(c))
            FOREACH (s IN $substances | MERGE (sub:Substance {name: s.name}) MERGE (k)-[r:INVOLVES]->(sub)
                SET r.amount = s.amount)
            FOREACH (p IN $people | MERGE (person:Person {name: p.name})
                SET person.aliases = coalesce(p.aliases, [])
                MERGE (person)-[r:INVOLVED_IN]->(k) SET r.role = p.role, r.charge = p.charge, r.sentence = p.sentence)
            """,
            name=case.get("name") or doc.metadata.get("title", doc.id),
            summary=case.get("summary", ""), date=case.get("date", ""), location=case.get("location", ""),
            charges=case.get("charges", []), people=[p for p in case.get("people", []) if p.get("name")],
            substances=[s for s in case.get("substances", []) if s.get("name")],
            doc_id=doc.id, title=doc.metadata.get("title", ""),
        )

    # ---------------------------------------------------------------- OWN ontology: writes

    OWN_KEYS = [("Article", "id"), ("Clause", "id"), ("Crime", "name"), ("Term", "name"), ("Substance", "name"),
                ("NewsArticle", "doc_id"), ("Case", "case_id"), ("Person", "pid"), ("Charge", "id"), ("Location", "name")]

    def own_constraints(self) -> None:
        for label, key in self.OWN_KEYS:
            self.run(f"CREATE CONSTRAINT IF NOT EXISTS FOR (n:{label}) REQUIRE n.{key} IS UNIQUE")

    def add_law_articles_own(self, articles: list[dict]) -> None:
        """All articles in one round trip per step (UNWIND) — the law KB is small but the link may be slow."""
        self.run(
            """
            UNWIND $articles AS art
            MERGE (a:Article {id: art.id}) SET a.title = art.title, a.law = art.law, a.doc_id = art.doc_id
            FOREACH (crime IN CASE WHEN art.crime IS NULL THEN [] ELSE [art.crime] END |
                MERGE (c:Crime {name: crime}) MERGE (a)-[:DEFINES]->(c))
            WITH a, art
            UNWIND art.clauses AS clause
            MERGE (cl:Clause {id: clause.id})
              SET cl.number = clause.number, cl.penalty = clause.penalty, cl.text = clause.text, cl.doc_id = art.doc_id,
                  cl.min_years = clause.min_years, cl.max_years = clause.max_years, cl.severity = clause.severity
            MERGE (a)-[:HAS_CLAUSE]->(cl)
            """,
            articles=[{k: v for k, v in a.items() if k != "terms"} for a in articles],
        )
        self.run(
            """
            UNWIND $rows AS t
            MATCH (cl:Clause {id: t.clause_id})
            MERGE (s:Substance {name: t.substance}) SET s.in_law = true
            MERGE (cl)-[r:THRESHOLD {point: t.point}]->(s)
              SET r.min_g = t.min_g, r.max_g = t.max_g, r.measure = t.measure
            """,
            rows=[{**t, "clause_id": c["id"]} for a in articles for c in a["clauses"] for t in c["thresholds"]],
        )
        self.run(
            """
            UNWIND $rows AS t
            MATCH (cl:Clause {id: t.clause_id})
            MERGE (term:Term {name: t.name}) SET term.definition = t.definition, term.doc_id = t.doc_id
            MERGE (cl)-[:DEFINES_TERM]->(term)
            """,
            rows=[{**t, "doc_id": a["doc_id"]} for a in articles for t in a["terms"]],
        )

    def link_other_substances(self) -> None:
        """Drugs the law does not name fall under 'chất ma túy khác (rắn|lỏng)' for threshold look-ups."""
        for name, state in OTHER_DRUG_STATE.items():
            self.run(
                """
                MATCH (s:Substance {name: $name}), (g:Substance {name: $group})
                MERGE (s)-[:FALLS_UNDER]->(g)
                """,
                name=name, group=f"chất ma túy khác ({state})",
            )

    def add_news_article_own(self, doc: Document) -> None:
        self.run("MERGE (n:NewsArticle {doc_id: $doc_id}) SET n.title = $title, n.name = $title, n.published = $published",
                 doc_id=doc.id, title=doc.metadata.get("title", ""), published=doc.metadata.get("document_version", ""))

    def add_case_own(self, case: dict, doc: Document, resolver: EntityResolver, index: int) -> None:
        people = []
        for p in case["people"]:
            pid = resolver.person(p["name"], p["aliases"])
            people.append({**p, "pid": pid,
                           "charges": [{**ch, "id": f"{doc.id}|{pid}|{ch['crime'] or ch['charge_text'].lower()}|{ch['stage']}"}
                                       for ch in p["charges"]]})
        accused = {p["pid"] for p in people if p["role"] in ACCUSED_ROLES or p["charges"]}
        case_id = resolver.case(accused, f"{doc.id}#{index}")
        self.run(
            """
            MATCH (n:NewsArticle {doc_id: $doc_id})
            MERGE (k:Case {case_id: $case_id})
              ON CREATE SET k.name = $name, k.summary = $summary, k.doc_ids = []
            SET k.doc_ids = CASE WHEN $doc_id IN k.doc_ids THEN k.doc_ids ELSE k.doc_ids + $doc_id END
            MERGE (n)-[rep:REPORTS]->(k) SET rep.stage = $stage, rep.date = $date
            FOREACH (loc IN CASE WHEN $location = '' THEN [] ELSE [$location] END |
                MERGE (l:Location {name: loc}) MERGE (k)-[:LOCATED_IN]->(l))
            FOREACH (crime IN $charges | MERGE (c:Crime {name: crime}) MERGE (k)-[:CHARGED_WITH]->(c))
            FOREACH (s IN $substances |
                MERGE (sub:Substance {name: s.name}) ON CREATE SET sub.in_law = false
                MERGE (k)-[r:INVOLVES]->(sub)
                SET r.amount = CASE WHEN r.amount_g IS NULL OR coalesce(s.amount_g, 0) > r.amount_g THEN s.amount ELSE r.amount END,
                    r.measure = CASE WHEN r.amount_g IS NULL OR coalesce(s.amount_g, 0) > r.amount_g THEN s.measure ELSE r.measure END,
                    r.amount_g = CASE WHEN r.amount_g IS NULL OR coalesce(s.amount_g, 0) > r.amount_g THEN s.amount_g ELSE r.amount_g END)
            WITH k
            UNWIND $people AS p
            MERGE (person:Person {pid: p.pid})
              ON CREATE SET person.name = p.name, person.aliases = [], person.doc_ids = []
            SET person.aliases = person.aliases + [a IN p.aliases WHERE NOT a IN person.aliases],
                person.doc_ids = CASE WHEN $doc_id IN person.doc_ids THEN person.doc_ids ELSE person.doc_ids + $doc_id END
            MERGE (person)-[inv:INVOLVED_IN]->(k) SET inv.role = p.role
            FOREACH (ch IN p.charges |
                MERGE (charge:Charge {id: ch.id})
                  SET charge.stage = ch.stage, charge.sentence = ch.sentence, charge.charge_text = ch.charge_text,
                      charge.doc_id = $doc_id
                MERGE (person)-[:FACES]->(charge)
                MERGE (charge)-[:IN_CASE]->(k)
                FOREACH (crime IN CASE WHEN ch.crime IS NULL THEN [] ELSE [ch.crime] END |
                    MERGE (c:Crime {name: crime}) MERGE (charge)-[:FOR_CRIME]->(c) MERGE (k)-[:CHARGED_WITH]->(c)))
            """,
            doc_id=doc.id, case_id=case_id, name=case.get("name") or doc.metadata.get("title", doc.id),
            summary=case.get("summary", ""), stage=case["stage"], date=case.get("date", ""),
            location=normalize_location(case.get("location", "")) if case.get("location") else "",
            charges=case["charges"], substances=case["substances"], people=people,
        )

    # ---------------------------------------------------------------- KG-3

    def context(self, question: str, doc_ids: list[str], max_facts: int = 60) -> list[str]:
        """Graph facts for a question: seeds + 1 hop, then the legal basis of every case reached."""
        if ontology_mode() == "hint":
            return self._context_hint(question, doc_ids, max_facts)
        return self._context_own(question, doc_ids, max_facts)

    def _context_own(self, question: str, doc_ids: list[str], max_facts: int = 60, max_seed_facts: int = 15) -> list[str]:
        """Own ontology. Order = priority: definitions, cases + people, legal basis, aggregation, then 1-hop seeds."""
        seed_ids, seed_facts = self.seed_facts(question, doc_ids, skip_labels=("Clause", "Term", "Charge"))
        facts: list[str] = []

        # 1. Definition questions ("tiền chất là gì?") -> Term -> defining clause.
        if re.search(r"là gì|nghĩa là|định nghĩa|được hiểu|khái niệm", question, re.IGNORECASE):
            terms = self.run("MATCH (t:Term) WHERE toLower($q) CONTAINS t.name "
                             "MATCH (cl:Clause)-[:DEFINES_TERM]->(t) RETURN t.name AS name, cl.id AS clause, t.definition AS d",
                             q=question)
            names = [t["name"] for t in terms]
            facts += [f"Định nghĩa '{t['name']}' ({t['clause']}): {t['d']}" for t in terms
                      if not any(t["name"] != other and t["name"] in other for other in names)]   # keep the longest term

        # 2. Focus cases: those of people named in the question, else those reported by the retrieved articles.
        named = self.run(
            """
            MATCH (p:Person)
            WHERE toLower($q) CONTAINS toLower(p.name)
               OR any(a IN p.aliases WHERE size(a) >= 3 AND toLower($q) CONTAINS toLower(a))
            RETURN p.pid AS pid
            """, q=question)
        named_pids = [r["pid"] for r in named]
        if named_pids:
            cases = self.run("MATCH (p:Person)-[:INVOLVED_IN]->(k:Case) WHERE p.pid IN $pids "
                             "RETURN DISTINCT k.case_id AS id, k.name AS name, k.summary AS summary", pids=named_pids)
        else:
            cases = self.run("MATCH (n:NewsArticle)-[:REPORTS]->(k:Case) WHERE n.doc_id IN $doc_ids "
                             "RETURN DISTINCT k.case_id AS id, k.name AS name, k.summary AS summary", doc_ids=doc_ids)
        case_ids = [k["id"] for k in cases]
        for k in cases:
            facts.append(f"Vụ việc '{k['name']}': {k['summary']}")
        for row in self.run(
            """
            MATCH (p:Person)-[inv:INVOLVED_IN]->(k:Case) WHERE k.case_id IN $ids
            OPTIONAL MATCH (p)-[:FACES]->(ch:Charge)-[:IN_CASE]->(k)
            OPTIONAL MATCH (ch)-[:FOR_CRIME]->(c:Crime)
            WITH k, p, inv, CASE WHEN p.pid IN $pids THEN 0 ELSE 1 END AS named_first,
                 collect(DISTINCT CASE WHEN ch IS NULL THEN NULL ELSE
                     coalesce(c.name, ch.charge_text) + ' [' + ch.stage + ']' +
                     CASE WHEN ch.sentence <> '' THEN ': ' + ch.sentence ELSE '' END END) AS charges
            RETURN k.name AS case, p.name AS person, p.aliases AS aliases, inv.role AS role, charges
            ORDER BY named_first, size(charges) DESC LIMIT 25
            """, ids=case_ids, pids=named_pids):
            alias = f" (biệt danh: {', '.join(row['aliases'])})" if row["aliases"] else ""
            charges = "; ".join(row["charges"]) or "chưa nêu tội danh"
            facts.append(f"{row['person']}{alias} - {row['role']} trong '{row['case']}' - buộc tội: {charges}")
        for row in self.run("MATCH (k:Case)-[i:INVOLVES]->(s:Substance) WHERE k.case_id IN $ids "
                            "RETURN k.name AS case, s.name AS s, i.amount AS amount", ids=case_ids):
            facts.append(f"'{row['case']}' liên quan chất {row['s']}" + (f": {row['amount']}" if row["amount"] else ""))

        # 3. Legal basis through the bridge: crimes of the named people (or of the case) -> Article -> clauses.
        crimes = self.run(
            """
            MATCH (k:Case) WHERE k.case_id IN $ids
            OPTIONAL MATCH (p:Person)-[:FACES]->(ch:Charge)-[:IN_CASE]->(k) WHERE p.pid IN $pids
            OPTIONAL MATCH (ch)-[:FOR_CRIME]->(pc:Crime)
            OPTIONAL MATCH (k)-[:CHARGED_WITH]->(kc:Crime)
            WITH k, collect(DISTINCT pc.name) AS person_crimes, collect(DISTINCT kc.name) AS case_crimes
            UNWIND CASE WHEN size(person_crimes) > 0 THEN person_crimes ELSE case_crimes END AS crime
            RETURN DISTINCT k.case_id AS case_id, crime
            """, ids=case_ids, pids=named_pids)
        legal: dict[str, str] = {}
        def add_clause(c: dict, why: str) -> None:
            legal.setdefault(c["id"], f"[{c['article']} - {c['title']}] khoản {c['number']} ({why}; khung: {c['penalty']}): {c['text']}")
        for pair in crimes:
            # 3a. threshold match: amount in the case vs [min_g, max_g) of each clause, for the drug or its law group
            for m in self.run(
                """
                MATCH (k:Case {case_id: $case_id})-[i:INVOLVES]->(s:Substance) WHERE i.amount_g IS NOT NULL
                MATCH (s)-[:FALLS_UNDER*0..1]->(ls:Substance)<-[t:THRESHOLD]-(cl:Clause)<-[:HAS_CLAUSE]-(a:Article)
                      -[:DEFINES]->(:Crime {name: $crime})
                WHERE t.measure = i.measure AND t.min_g <= i.amount_g AND (t.max_g IS NULL OR i.amount_g < t.max_g)
                RETURN s.name AS s, i.amount AS amount, i.amount_g AS grams, ls.name AS group, t.point AS point,
                       t.min_g AS low, t.max_g AS high, t.measure AS measure,
                       cl.id AS id, a.id AS article, a.title AS title, cl.number AS number, cl.penalty AS penalty, cl.text AS text
                """, **pair):
                unit = "g" if m["measure"] == "mass" else "ml"
                bound = f"từ {m['low']:g}{unit} đến dưới {m['high']:g}{unit}" if m["high"] else f"từ {m['low']:g}{unit} trở lên"
                via = "" if m["group"] == m["s"] else f" (thuộc nhóm '{m['group']}')"
                facts.append(f"Đối chiếu khối lượng: {m['s']}{via} {m['amount']} (= {m['grams']:g}{unit}) {bound} "
                             f"-> {m['article']} khoản {m['number']} điểm {m['point']}: {m['penalty']}")
                add_clause(m, f"áp dụng theo khối lượng {m['s']}")
            # 3b. basic frame (khoản 1) and the most severe frame of the article
            for c in self.run(
                """
                MATCH (a:Article)-[:DEFINES]->(:Crime {name: $crime}) MATCH (a)-[:HAS_CLAUSE]->(cl:Clause)
                WITH a, cl ORDER BY cl.severity DESC
                WITH a, collect(cl) AS cls
                UNWIND [x IN cls WHERE x.number = 1] + [cls[0]] AS cl
                RETURN DISTINCT cl.id AS id, a.id AS article, a.title AS title, cl.number AS number,
                       cl.penalty AS penalty, cl.text AS text, cl.severity AS severity
                """, crime=pair["crime"]):
                add_clause(c, "khung cơ bản" if c["number"] == 1 else "khung nặng nhất")

        # 4. Articles named in the question ("Điều 251"): basic + most severe frame.
        for number in re.findall(r"[Đđ]iều (\d+)", question):
            for c in self.run(
                """
                MATCH (a:Article)-[:HAS_CLAUSE]->(cl:Clause) WHERE a.id STARTS WITH $prefix
                WITH a, cl ORDER BY cl.severity DESC
                WITH a, collect(cl) AS cls
                UNWIND [x IN cls WHERE x.number = 1] + [cls[0]] AS cl
                RETURN DISTINCT cl.id AS id, a.id AS article, a.title AS title, cl.number AS number,
                       cl.penalty AS penalty, cl.text AS text
                """, prefix=f"Điều {number} "):
                add_clause(c, "khung cơ bản" if c["number"] == 1 else "khung nặng nhất")
        facts += list(legal.values())

        # 5. Aggregation ("những vụ nào liên quan MDMA?"): no person named -> every case with that substance.
        if not named_pids:
            for row in self.run(
                """
                MATCH (s:Substance) WHERE size(s.name) >= 3 AND toLower($q) CONTAINS toLower(s.name)
                MATCH (k:Case)-[i:INVOLVES]->(s)
                OPTIONAL MATCH (p:Person)-[:INVOLVED_IN]->(k)
                RETURN s.name AS s, k.name AS case, i.amount AS amount, k.doc_ids AS docs,
                       collect(DISTINCT p.name)[..6] AS people
                """, q=question):
                who = f" (người liên quan: {', '.join(row['people'])})" if row["people"] else ""
                facts.append(f"Vụ có {row['s']}: '{row['case']}'{who}" + (f", khối lượng {row['amount']}" if row["amount"] else ""))

        return (list(dict.fromkeys(facts)) + seed_facts[:max_seed_facts])[:max_facts]

    def _context_hint(self, question: str, doc_ids: list[str], max_facts: int = 60) -> list[str]:
        """Suggested ontology (baseline for ket_qua_benchmark_kg.hint.txt)."""
        seed_ids, facts = self.seed_facts(question, doc_ids, skip_labels=("Clause",))
        cases = self.run(
            """
            MATCH (k:Case)
            WHERE elementId(k) IN $ids OR EXISTS { MATCH (s)--(k) WHERE elementId(s) IN $ids }
            RETURN elementId(k) AS id, k.name AS name, k.summary AS summary
            """,
            ids=seed_ids,
        )
        facts += [f"Vụ việc '{k['name']}': {k['summary']}" for k in cases]
        clauses = self.run(
            """
            MATCH (k:Case)-[:CHARGED_WITH]->(:Crime)<-[:DEFINES]-(a:Article)-[:HAS_CLAUSE]->(cl:Clause)
            WHERE elementId(k) IN $case_ids
              AND (cl.number = 1 OR EXISTS { (k)-[:INVOLVES]->(:Substance)<-[:MENTIONS]-(cl) })
            RETURN DISTINCT a.id AS article, a.title AS title, cl.number AS number, cl.text AS text
            UNION
            MATCH (a:Article)-[:HAS_CLAUSE]->(cl:Clause)
            WHERE a.id IN $named
              AND (cl.number = 1 OR EXISTS { (cl)-[:MENTIONS]->(s:Substance) WHERE s.name IN $subs })
            RETURN DISTINCT a.id AS article, a.title AS title, cl.number AS number, cl.text AS text
            """,
            case_ids=[k["id"] for k in cases],
            named=[f"Điều {n} BLHS" for n in re.findall(r"[Đđ]iều (\d+)", question)],
            subs=find_substances(question),
        )
        legal = [f"[{c['article']} - {c['title']}] khoản {c['number']}: {c['text']}" for c in clauses]
        return (legal + facts)[:max_facts]   # multi-hop legal facts first so the 1-hop seed edges cannot crowd them out

# ---------------------------------------------------------------------------------------------- KG-2

def build_graph(graph: Neo4jGraph, law_docs: list[Document], news_docs: list[Document],
                llm_fn: Callable[..., str]) -> None:
    """Load both KBs into an empty graph. llm_fn(prompt, json_mode=False) -> str (metered OpenAI chat)."""
    if ontology_mode() == "hint":
        return _build_graph_hint(graph, law_docs, news_docs, llm_fn)
    graph.own_constraints()
    articles = [parse_law_article_own(d) for d in law_docs]               # regex: clauses, thresholds, terms
    graph.add_law_articles_own(articles)
    crimes = [a["crime"] for a in articles if a["crime"]]
    resolver = EntityResolver()
    for d in news_docs:
        graph.add_news_article_own(d)
        for i, case in enumerate(extract_news_own(d, lambda p: llm_fn(p, json_mode=True), crimes)):
            graph.add_case_own(case, d, resolver, i)
    graph.link_other_substances()

def _build_graph_hint(graph: Neo4jGraph, law_docs: list[Document], news_docs: list[Document],
                      llm_fn: Callable[..., str]) -> None:
    """Suggested ontology (baseline): the HINT helpers, unchanged."""
    graph.suggested_constraints()
    articles = [parse_law_article(d) for d in law_docs]
    for a in articles:
        graph.add_law_article(a)
    crimes = [a["crime"] for a in articles if a["crime"]]
    for d in news_docs:
        for case in extract_news_cases(d, lambda p: llm_fn(p, json_mode=True), crimes):
            graph.add_news_case(case, d)

# ---------------------------------------------------------------------------------------------- KG-4

GRAPH_PROMPT = """Trả lời câu hỏi chỉ dựa trên ngữ cảnh (đoạn văn bản và dữ kiện từ knowledge graph).
Nêu rõ số Điều luật khi có. Nếu ngữ cảnh không đủ, nói không đủ thông tin.

Dữ kiện knowledge graph:
{facts}

Đoạn văn bản:
{chunks}

Câu hỏi: {question}
Trả lời:"""

class GraphRAGAgent:
    """Hybrid GraphRAG: the same vector top-k as flat RAG, plus facts expanded from the graph."""

    def __init__(self, store: EmbeddingStore, graph: Neo4jGraph, llm_fn: Callable[[str], str]) -> None:
        self.store = store
        self.graph = graph
        self.llm_fn = llm_fn

    def answer(self, question: str, top_k: int = 3) -> str:
        chunks = self.store.search(question, top_k=top_k)
        doc_ids = list(dict.fromkeys(c["metadata"]["doc_id"] for c in chunks if c.get("metadata", {}).get("doc_id")))
        facts = self.graph.context(question, doc_ids)
        prompt = GRAPH_PROMPT.format(
            facts="\n".join(f"- {f}" for f in facts) or "- (không có)",
            chunks="\n\n".join(f"[{i}] {c['content']}" for i, c in enumerate(chunks, start=1)),
            question=question,
        )
        return self.llm_fn(prompt)
