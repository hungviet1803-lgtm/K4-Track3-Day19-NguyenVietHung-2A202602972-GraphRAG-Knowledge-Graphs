# Báo cáo Day 19 — Flat RAG vs GraphRAG

**Họ tên:** Nguyễn Việt Hưng  **MSSV:** 2A202602972  **Ngày:** 2026-10-05

> Kỳ vọng và thang điểm: `SUBMISSION.md`. Mọi số liệu dưới đây lấy từ `ket_qua_benchmark_kg.txt` (ontology tự thiết kế, `report/ONTOLOGY.md`). Số liệu của ontology gợi ý nằm trong `ket_qua_benchmark_kg.hint.txt` và chỉ dùng để so sánh.

**Môi trường chạy (ảnh hưởng tới cách đọc số liệu):**
- Chat `gemini-3.5-flash-lite`, embedding `gemini-embedding-001` (key Gemini). Model mặc định `gemini-2.5-flash-lite` trả 404 cho tài khoản mới, nên model chat được đổi qua `GEMINI_CHAT_MODEL` trong `.env`.
- Giá chat lấy từ trang giá chính thức của Google ($0.30 / $2.50 cho 1M token input/output), thêm vào bảng giá `src/llm.py`. **API embedding của Gemini không trả số token**, nên mọi chi phí embedding hiện là 0 (`flat` indexing: `in_tok = 0`, `USD = 0`). Chi phí Flat RAG vì vậy bị **đánh giá thấp**.
- Neo4j chạy trên **Neo4j Aura** (Hong Kong) thay vì Docker: Docker Desktop không tải được image `neo4j:5` (proxy nội bộ của Docker đứng yên ở "Pulling fs layer"; tải trực tiếp bằng curl chỉ đạt khoảng 80–100 KB/s). Mạng lúc chạy có ping khoảng 470 ms, nên **cột `seconds` của pipeline graph bị nhiễu mạng** (xem mục 1).

## 1. Chi phí (10 điểm)

```
Chat model: gemini:gemini-3.5-flash-lite | Embedding: gemini:gemini-embedding-001 | top_k=3 | chunk_size=800 | chunks=176 | KG: 259 nodes / 579 rels

== Indexing (one-off)
pipeline  calls    in_tok  out_tok       USD  seconds
flat        176         0        0   0.00000    168.3
graph       196     38590     5949   0.02645    271.3

== Querying (mean per question)
pipeline  recall  judge   in_tok  out_tok       USD  seconds
flat        0.51   1.50      696       70   0.00038     3.59
graph       0.94   2.00     2102      118   0.00093    27.53
```

| Chỉ số | Flat | Graph | Graph / Flat |
| --- | --- | --- | --- |
| Indexing USD | 0.00000 (embedding không đo được) | 0.02645 | không xác định (chia cho 0); toàn bộ $0.02645 là chi phí dựng KG |
| Indexing giây | 168.3 | 271.3 | ×1.61 |
| Mỗi câu: USD | 0.00038 | 0.00093 | ×2.45 |
| Mỗi câu: giây | 3.59 | 27.53 | ×7.67 (bị nhiễu mạng, xem dưới) |
| Mỗi câu: in_tok | 696 | 2102 | ×3.02 |

**Chi phí tăng thêm đến từ đâu?**
> **Indexing:** pipeline graph = cùng 176 lần embed như Flat + **20 lần gọi LLM trích xuất** (196 − 176), mỗi lần là 1 bài báo (khoảng 1.930 token vào, 300 token ra). Toàn bộ $0.02645 và khoảng 103 giây chênh lệch (271.3 − 168.3) là chi phí trích xuất bằng LLM cộng với ghi vào Neo4j. Phần luật trích bằng regex nên không tốn token nào.
>
> **Mỗi câu hỏi:** prompt dài gấp 3 lần (+1.406 token) vì có thêm dữ kiện graph (vụ, người, buộc tội, khoản luật, đối chiếu ngưỡng). Output dài hơn khoảng 48 token vì câu trả lời có Điều/khoản. Đây là toàn bộ chênh lệch USD (+$0.00055/câu).
>
> **Độ trễ ×7.67 chủ yếu không do GraphRAG.** Truy vấn trong lượt benchmark không gặp lỗi 429 nào (`[rate-limit]` = 0 lần). Mình đo lại `context()` cho từng câu sau benchmark: mỗi câu cần 8–10 truy vấn Cypher, mỗi truy vấn 0,1–0,6 giây, cả câu Q5 chỉ mất **2,9–3,6 giây**. Trong lượt benchmark, chính Q5 mất 16 giây và lần đo đầu mất 96 giây. Vậy phần lớn độ trễ là **mạng tới Aura chập chờn**: lượt chạy ontology gợi ý (cùng kiểu truy vấn, ít truy vấn hơn) chỉ mất 4.21 giây/câu ở một thời điểm mạng tốt hơn. Muốn so độ trễ cho đúng phải chạy Neo4j cục bộ.

**Điểm hòa vốn.** Tính theo USD thì GraphRAG **không bao giờ rẻ hơn** Flat (mỗi câu đắt hơn $0.00055, cộng $0.02645 trả một lần). Câu hỏi đúng hơn là: *mỗi câu trả lời đúng tốn bao nhiêu?* Trên 6 câu, Flat có 3 câu judge = 2 (và Q6 là judge = 2 sai, xem E4), Graph có 6/6 câu judge = 2. Phần dựng KG trải ra trên N câu hỏi: N = 6 thì mỗi câu đắt thêm $0.0050; N = 100 thì chỉ còn $0.00081 (= 0.02645/100 + 0.00055). Với loại câu `cross-kb`, Flat **không thể** trả lời đủ dù có tốn bao nhiêu (ngữ cảnh không có Điều luật), nên KG hòa vốn ngay khi có câu hỏi xuyên KB đầu tiên mà người dùng cần đáp án đúng.

**So với ontology gợi ý (`ket_qua_benchmark_kg.hint.txt`):** indexing đắt hơn 9% ($0.02645 so với $0.02431, vì prompt trích xuất dài hơn), nhưng mỗi câu hỏi rẻ hơn 52% ($0.00093 so với $0.00194; in_tok 2102 so với 5234) vì không đưa mọi khoản nhắc tới chất vào prompt. Phần indexing đắt thêm $0.00214 được bù lại sau khoảng 2 câu hỏi (0.00214 / 0.00101).

## 2. Từng câu hỏi (10 điểm)

| Câu | Loại | Flat recall / judge | Graph recall / judge | Thắng | Vì sao (1 câu) |
| --- | --- | --- | --- | --- | --- |
| Q1 | single-hop-law | 1.00 / 2 | 1.00 / 2 | Hòa | Định nghĩa "tiền chất" nằm gọn trong một chunk (Điều 2 PCMT khoản 4); graph chỉ thêm nguồn "khoản 4 Điều 2". |
| Q2 | single-hop-news | 1.00 / 2 | 1.00 / 2 | Hòa | Tên 2 bị cáo tử hình nằm ngay câu sapo của bài, vector search lấy đủ. |
| Q3 | cross-kb | 0.33 / 1 | 1.00 / 2 | **Graph** | Flat có "36 tháng" nhưng *"Ngữ cảnh không đủ thông tin để biết tội đó được quy định tại điều nào"*; graph đi `Person→Charge→Crime←Article→Clause{1}` ra Điều 251, khoản 1 từ 02 đến 07 năm. |
| Q4 | cross-kb | 0.33 / 1 | 1.00 / 2 | **Graph** | Flat thiếu Điều luật; graph lấy khoản `severity` cao nhất của Điều 255: *"20 năm hoặc tù chung thân"*. |
| Q5 | cross-kb-multi-hop | 0.40 / 1 | 1.00 / 2 | **Graph** | Flat: *"Khung hình phạt và khoản của điều luật áp dụng: Ngữ cảnh không đủ thông tin"*; graph so 9.600 g với `THRESHOLD` ra Điều 250 khoản 4 điểm b. |
| Q6 | aggregation | 0.00 / 2 | 0.67 / 2 | **Graph** (judge chấm sai Flat) | Flat chỉ thấy top-3 chunk: đếm 2 chuyến hàng của Huy thành 2 vụ, sót vụ Viện Pháp y; graph truy vấn mọi `Case-[:INVOLVES]->(:Substance{name:'MDMA'})` ra đủ 4 vụ (xem E4 vì sao recall chỉ 0.67). |

**Quy luật:** câu hỏi mà đáp án nằm trong **một** đoạn văn (single-hop: Q1, Q2) thì Flat hòa Graph với chi phí và độ trễ thấp hơn. Graph thắng ở mọi câu cần **nối hai nguồn** (cross-kb: Q3–Q5, recall trung bình Flat 0.35 so với Graph 1.00) hoặc cần **quét toàn kho** (aggregation: Q6), vì top-k = 3 chunk không thể chứa đủ. Câu càng nhiều bước (Q5: người → tội → Điều → khoản theo khối lượng) thì Flat càng hụt.

## 3. Phân tích lỗi (20 điểm)

### Lỗi E2: Thiếu ngữ cảnh luật — sai khung tối đa (ontology gợi ý, Q4)

- **Hiện tượng:** với ontology gợi ý, GraphRAG trả lời sai mức phạt tối đa cho "Hoàng Nato", dù graph có đủ 5 khoản của Điều 255.
- **Bằng chứng:** `ket_qua_benchmark_kg.hint.txt`, Q4 graph, recall 0.67, judge 1:

```
- **Mức phạt tù:** Theo **Điều 255 BLHS (Tội tổ chức sử dụng trái phép chất ma túy) khoản 1**, người nào tổ chức sử dụng
trái phép chất ma túy dưới bất kỳ hình thức nào thì bị phạt tù từ 02 năm đến 07 năm. *(Lưu ý: Ngữ cảnh và knowledge graph
chỉ cung cấp quy định tại khoản 1 của Điều 255 BLHS với mức phạt tù tối đa là 07 năm).*
```

Đáp án chuẩn: khoản 4 Điều 255, tù 20 năm hoặc chung thân. Graph có khoản này (`MATCH (a:Article {id:'Điều 255 BLHS'})-[:HAS_CLAUSE]->(cl) RETURN cl.number, cl.penalty` → 5 khoản, khoản 4 = "phạt tù 20 năm hoặc tù chung thân").

- **Nguyên nhân:** quy tắc lọc ở KG-3 (bản gợi ý): *khoản 1 + những khoản `MENTIONS` một `Substance` mà vụ `INVOLVES`*. Điều 255 không có ngưỡng chất nào (0 điểm có "gam"), còn chất trong vụ là etomidate, cũng không có trong luật, nên chỉ khoản 1 qua được bộ lọc. Lỗi nằm ở **thiết kế ontology** (không có thuộc tính nào cho biết khoản nào nặng nhất) và **Cypher KG-3** (lọc theo chất thay vì theo loại câu hỏi). Thêm nữa, LLM đã trung thực khi nói "chỉ cung cấp khoản 1", nhưng vẫn kết luận sai "tối đa 07 năm".
- **Đề xuất sửa (đã làm ở ontology riêng):** thêm `Clause.severity` (regex từ `penalty`: tử hình = 100, chung thân = 50, còn lại = số năm tối đa). KG-3 luôn lấy khoản 1 và khoản có `severity` cao nhất cho mỗi Điều đi tới. **Kết quả:** Q4 graph trong `ket_qua_benchmark_kg.txt` đạt recall 1.00, judge 2: *"Theo khoản 4 Điều này, mức phạt tù tối đa là **20 năm hoặc tù chung thân**"*. Đánh đổi: thêm 1 khoản mỗi Điều (khoảng 150–600 token). Cách khác là lấy hết mọi khoản, nhưng tốn gấp khoảng 4 lần.

### Lỗi E3: Trùng thực thể — một vụ thành nhiều node (ontology gợi ý, Q6)

- **Hiện tượng:** với ontology gợi ý, câu tổng hợp Q6 liệt kê cùng một vụ nhiều lần như thể là các vụ khác nhau.
- **Bằng chứng:** `ket_qua_benchmark_kg.hint.txt`, Q6 graph:

```
1. **Vụ vận chuyển hơn 10kg ma túy từ Đức về Việt Nam qua sân bay Nội Bài** (... Cái Quang Huy, Nguyễn Tiến Đạt ...)
3. **Vụ vận chuyển trái phép chất ma túy qua sân bay Nội Bài do Cái Quang Huy thực hiện** (... hơn 9,6kg ...)
4. **Vụ án sai phạm tại Viện Pháp y tâm thần Trung ương** (có liên quan đến MDMA).
5. **Vụ tổ chức sử dụng trái phép chất ma túy và trốn viện tại Sầm Sơn và Viện Pháp y tâm thần Trung ương** (... Lê Văn Đông).
```

Mục 1 và 3 cùng là vụ Cái Quang Huy; mục 4 và 5 cùng là vụ Viện Pháp y (bài `news-100260924105118645` và `news-100260930085028036`). recall vẫn là 1.00 vì từ khóa có xuất hiện, nên **con số tổng hợp che mất lỗi**.

- **Nguyên nhân:** **thiết kế ontology**, cụ thể là khóa định danh. `add_news_case` làm `MERGE (k:Case {name: $name})` với `name` do LLM tự đặt cho từng bài; hai bài về cùng vụ có hai tên khác nhau nên thành hai node. `Person` khóa theo tên nhưng biệt danh chỉ là thuộc tính, không phải khóa. Riêng mục 1 và 3 còn do **crawl**: bài Lê Minh Thành có dính sapo bài Cái Quang Huy ở cuối, nên LLM trích thêm một vụ Huy từ bài đó. Kiểm chứng bằng cách chạy lại `extract_news_cases` (hàm HINT) trên riêng bài `news-100260918080821054`:

```
Vụ mua bán trái phép chất ma túy do Lê Minh Thành và đồng phạm thực hiện tại Hà Nội | ['Lê Minh Thành', 'Trịnh Vũ Kiên', 'Kim Xuân Tuấn', 'Nguyễn Quang Hưng']
Vụ vận chuyển ma túy qua sân bay Nội Bài do Cái Quang Huy thực hiện                  | ['Cái Quang Huy']   <- chỉ có trong đoạn sapo cuối bài
```

Khảo sát 20 bài: 17 bài có đoạn cuối là sapo của bài khác (8 bài khớp nguyên văn sapo của một bài khác trong corpus).
- **Đề xuất sửa (đã làm):** (1) `Person.pid` = họ tên chuẩn hóa, mọi biệt danh trỏ về cùng `pid`; (2) `Case.case_id` gán trong code: vụ mới dùng chung ít nhất một người bị buộc tội với vụ cũ thì gộp; (3) bỏ đoạn sapo cuối bài khi trích xuất. **Kết quả trên graph cuối:**

```cypher
MATCH (k:Case) RETURN k.name AS name, size(k.doc_ids) AS n_docs ORDER BY n_docs DESC;
```
```
'Vụ bắt Dương Minh Tuấn cùng đồng phạm trong 8 đường dây ma túy'     4
'Vụ án tại Viện Pháp y tâm thần Trung ương'                          2
'Vụ Cái Quang Huy và Nguyễn Tiến Đạt vận chuyển ... từ Đức về Việt Nam' 1
... (8 vụ, không vụ nào trùng)
```
```cypher
MATCH (p:Person {name:'Dương Minh Tuấn'}) RETURN p.aliases, size(p.doc_ids);   // ['Hoàng Nato'], 4
```

Q6 graph bản mới chỉ còn 4 vụ khác nhau. Đánh đổi: hai người trùng họ tên ở hai vụ khác nhau sẽ bị gộp nhầm. Kho lớn cần thêm năm sinh hoặc quê quán vào khóa, hoặc một bước LLM so khớp (tốn thêm token).

### Lỗi E4: Phép đo sai — recall và judge mâu thuẫn (Q6, cả hai pipeline)

- **Hiện tượng:** ở Q6, hai thước đo cho kết quả ngược nhau theo cả hai chiều.
  - Flat: **recall 0.00 nhưng judge 2** (đúng đủ).
  - Graph: **recall 0.67 nhưng judge 2**.
- **Bằng chứng:** `ket_qua_benchmark_kg.txt`.
  - Q6 flat (recall=0.00, judge=2): *"cả 3 vụ việc đều có liên quan đến ma túy MDMA: 1. … thùng hàng chứa các viên nén màu xanh là MDMA (gần 4,3kg) liên quan đến Đạt và Huy. 2. Thành bị bắt quả tang … 3. … viên nén hình tam giác màu hồng - xám … (hơn 5,3kg) liên quan đến Đức."* Mục 1 và 3 thực ra là **hai chuyến hàng của cùng vụ Cái Quang Huy**, và câu trả lời **thiếu vụ Viện Pháp y** (gold có 3 vụ). Judge vẫn cho 2, tức **judge sai**: quá dễ dãi.
  - Q6 graph (recall=0.67, judge=2): *"2. 'Vụ Trịnh Vũ Kiên và đồng phạm mua bán trái phép chất ma túy' (liên quan đến 5 viên MDMA)"*. Đây chính là vụ của Lê Minh Thành (`MATCH (p:Person)-[:INVOLVED_IN]->(k:Case {name:'Vụ Trịnh Vũ Kiên và đồng phạm mua bán trái phép chất ma túy'}) RETURN p.name` → Lê Minh Thành, Trịnh Vũ Kiên, Kim Xuân Tuấn, Nguyễn Quang Hưng), nhưng `must_include` đòi chuỗi "Lê Minh Thành", nên **recall phạt oan**.
  - Câu trả lời graph còn có vụ thứ 4 "Vụ bắt Dương Minh Tuấn … 8 đường dây". Bài gốc ghi các đường dây mua bán *"etomidate, ketamine, thuốc lắc"*, và "thuốc lắc" chính là MDMA, nên vụ này **có liên quan MDMA thật** nhưng không có trong `gold`. Không thước đo nào nhận ra điều này.
- **Nguyên nhân:** nằm ở **phép đo**, không ở pipeline. (1) `keyword_recall` so chuỗi con, nên một vụ được nhắc bằng tên khác (tên vụ, tên đồng phạm) bị tính là sai. (2) LLM-judge không đối chiếu từng mục với đáp án, nên chấp nhận câu trả lời thiếu hoặc đếm sai. (3) `gold` của Q6 chưa đủ (thiếu vụ "thuốc lắc"). Ngoài ra còn một phần ở **prompt trích xuất của mình**: tên vụ do LLM đặt theo người đứng đầu danh sách bị cáo trong bài (Kiên), không phải người chính (Thành).
- **Đề xuất sửa:** (1) chấm Q6 theo **tập vụ**: map mỗi vụ trong câu trả lời về `case_id` hoặc tên người chính rồi tính precision/recall trên tập. (2) Prompt judge yêu cầu liệt kê từng ý của gold là "có / thiếu / sai" trước khi cho điểm. (3) Bổ sung gold Q6 hoặc ghi rõ "chỉ tính MDMA được giám định". (4) Phía pipeline: thêm `Case.main_person` (người có mức án nặng nhất) vào tên vụ trong `context()`. Đánh đổi: judge chi tiết hơn sẽ tốn thêm token cho mỗi lần chấm.

### Lỗi E1 (biến thể): Cầu nối **sai** — tội ngoài Chương XX bị ép vào tội trong danh sách

- **Hiện tượng:** E1 kinh điển (vụ không nối được sang luật) chỉ xảy ra ở 2 vụ, và cả 2 lần đều **gãy hợp lý**. Lỗi nguy hiểm hơn là **nối sai**.
- **Bằng chứng:**

```cypher
MATCH (k:Case) WHERE NOT (k)-[:CHARGED_WITH]->() RETURN k.name, k.doc_ids;
```
```
'Vụ bắt giữ 7 công dân Trung Quốc vận chuyển hơn 800kg chất nghi ma túy và vũ khí tại Campuchia'  ['news-100260924145818945']
'Vụ triệt phá chuyên án A3-626P'                                                                   ['news-100261002184934505']
```

Bài gốc: vụ Campuchia do cảnh sát Campuchia xử lý (không thuộc BLHS Việt Nam); chuyên án A3-626P chỉ ghi *"bắt giữ 2 đối tượng, thu giữ 40kg ma túy"*, không nêu tội. **Không nối là đúng**: đoán tội còn tệ hơn.

```cypher
MATCH (p:Person {name:'Nguyễn Thị Mai Anh'})-[:FACES]->(ch:Charge)-[:FOR_CRIME]->(c:Crime) RETURN ch.stage, c.name, ch.doc_id;
// 'xét xử sơ thẩm', 'mua bán trái phép chất ma túy', 'news-100260924105118645'
MATCH (ch:Charge) WHERE NOT (ch)-[:FOR_CRIME]->() RETURN count(ch);   // 0
```

Bài `news-100260924105118645` viết Mai Anh bị cáo buộc *"đưa tiền cho lãnh đạo, bác sĩ, điều dưỡng và môi giới 'chạy' kết luận giám định tâm thần"* (hối lộ); người *"tiếp tục mua bán trái phép chất ma túy"* là Bùi Thị Thanh Thủy. Graph nối Mai Anh sang Điều 251 là **sai**. Câu hỏi "Mai Anh bị xử theo Điều nào?" sẽ ra Điều 251.

- **Nguyên nhân:** **prompt trích xuất + thiết kế danh sách tội**. Prompt đưa 13 tội Chương XX làm danh sách chuẩn và cho phép ghi nguyên văn tội ngoài danh sách. Nhưng LLM (gemini-3.5-flash-lite) vẫn chọn một tội trong danh sách cho mọi người (0 `Charge` không có `FOR_CRIME`, kể cả các tội nhận/đưa hối lộ, đánh bạc mà bài nêu rõ). `link_entity` không bắt được lỗi này, vì chuỗi LLM trả về khớp chính xác một tội chuẩn.
- **Đề xuất sửa:** (1) yêu cầu LLM trả thêm `evidence` (câu trích nguyên văn chứa tội danh của người đó), rồi kiểm tra trong code rằng tội có xuất hiện trong câu trích; không xuất hiện thì bỏ `FOR_CRIME`, chỉ giữ `charge_text`. (2) Thêm một mục "tội ngoài Chương XX" vào danh sách để LLM có lối thoát hợp lệ. Đánh đổi: thêm khoảng 20% token output khi trích xuất; một số buộc tội đúng nhưng diễn đạt gián tiếp sẽ bị bỏ (giảm recall của cầu nối).

### Lỗi E6: Thuộc tính thiếu — `sentence` rỗng và bị cáo không có buộc tội

```cypher
MATCH (:Person)-[:FACES]->(ch:Charge)
RETURN ch.stage, sum(CASE WHEN ch.sentence = '' THEN 1 ELSE 0 END) AS empty, count(*) AS total;
```
```
'bắt giữ'          16 / 16     -> hợp lý: chưa xét xử thì chưa có án
'truy tố'           2 /  2     -> hợp lý (Cái Quang Huy, Nguyễn Tiến Đạt: phiên tòa dự kiến 29-9)
'xét xử sơ thẩm'    4 / 14     -> Mai Anh, Lê Văn Đông (×3): phiên tòa Viện Pháp y đang diễn ra, chưa tuyên án → hợp lý
```

Nhờ tách `stage` (ontology riêng), có thể phân biệt *thiếu hợp lý* với *lỗi trích xuất*. Ở ontology gợi ý, `INVOLVED_IN.sentence` rỗng không cho biết người đó đang ở giai đoạn nào. **Lỗi thật** là 3 người có vai trò bị cáo nhưng không có buộc tội nào:

```cypher
MATCH (p:Person)-[i:INVOLVED_IN]->(k:Case) WHERE k.name CONTAINS 'Pháp y' AND NOT (p)-[:FACES]->() RETURN p.name, i.role;
// Ngô Việt Dũng 'bị cáo', Trần Quốc An 'bị cáo', Cao Thị Bích Hằng 'bị cáo'
```

Bài `news-100260930085028036` mô tả hành vi (Dũng *"đưa đồ uống có ma túy và ống thủy tinh chứa cần sa cho người khác sử dụng"*) nhưng không ghi tên tội của từng người. Đây là thiếu ở **nguồn**, không phải lỗi trích xuất. Sửa: cho phép `Charge` dạng "hành vi" (không có `FOR_CRIME`) để graph vẫn giữ hành vi; không nên để LLM tự suy ra tội từ hành vi (dẫn tới đúng loại lỗi đã nêu ở mục E1 biến thể).

## 4. Kết luận (5 điểm)

> **Nên dùng KG khi** câu hỏi cần nối dữ kiện từ hai nguồn có cấu trúc khác nhau, hoặc cần quét toàn kho. Trên 3 câu `cross-kb` (Q3–Q5), Flat RAG chỉ đạt recall trung bình 0.35 và judge 1; GraphRAG đạt 1.00 và judge 2 ở cả ba. Lý do không phải LLM tốt hơn mà là ngữ cảnh: top-3 chunk không bao giờ chứa cả mức án (tin tức) lẫn khung phạt (luật). Flat tự nói *"Ngữ cảnh không đủ thông tin"*. Với câu tổng hợp (Q6), Flat đếm sai số vụ, còn graph trả về đúng tập vụ bằng một truy vấn.
>
> **Flat RAG là đủ khi** đáp án nằm trong một đoạn văn (Q1, Q2: hai bên hòa, recall 1.00 / judge 2), với chi phí bằng 41% mỗi câu ($0.00038 so với $0.00093), prompt ngắn hơn 3 lần, và không tốn $0.02645 dựng KG.
>
> **Điều kiện cụ thể để KG đáng tiền:** (1) có ít nhất một KB **đủ đều để trích bằng regex** (ở đây luật: 13 Điều, 281 ngưỡng, 0 token), nên phần đắt chỉ còn trích xuất tin (20 lần gọi LLM); (2) tỉ lệ câu hỏi xuyên KB hoặc tổng hợp đáng kể (ở đây 4/6 câu); (3) số câu hỏi đủ lớn để trải chi phí dựng ($0.02645 / 100 câu = $0.00026/câu). **Thiết kế ontology quan trọng ngang việc có KG:** cùng dữ liệu, cùng LLM, ontology riêng giảm 60% token mỗi câu so với ontology gợi ý (2102 so với 5234) và sửa được Q4 (judge 1 → 2), vì đưa vào prompt **đúng** khoản (ngưỡng tính bằng Cypher, khung nặng nhất) thay vì mọi khoản nhắc tới chất.
>
> **Rủi ro cần nhớ:** KG chỉ tốt bằng phần trích xuất. Graph này nối Mai Anh sang Điều 251 sai (E1 biến thể) và gán "ma túy tổng hợp" thành Methamphetamine. Graph trình bày những dữ kiện sai đó cũng tự tin như dữ kiện đúng, nên cần kiểm tra trích xuất bằng câu trích dẫn trước khi dùng cho câu hỏi pháp lý.

## 5. Tự kiểm (5 điểm)

```
$ pytest tests/ -q
................................................                         [100%]
48 passed in 0.11s

$ python bench_kg.py --check
[OK] Dữ liệu: 18 điều luật, 20 bài báo
[OK] KG-1 link_entity
[OK] Neo4j kết nối được
[provider] chat = gemini:gemini-3.5-flash-lite | embedding = gemini:gemini-embedding-001
[OK] KG-2 build_graph: 174 node / 429 cạnh, đường xuyên 2 KB dài 2 cạnh
[OK] KG-3 context: 11 dữ kiện, có Điều 251
[OK] KG-4 GraphRAGAgent.answer
[OK] Chi phí check: 1 lần gọi LLM, $0.00234. Graph nhỏ (luật + 1 bài) vẫn còn trong Neo4j để bạn xem; chạy --judge để dựng graph đầy đủ.
```

Ảnh Neo4j (chụp trên Neo4j Browser `browser.neo4j.io`, kết nối tới instance Aura, trên graph đầy đủ sinh bởi lượt `--judge` của `ket_qua_benchmark_kg.txt`, 259 node): `report/img/kg_count.png`, `report/img/kg_cross_kb.png`, `report/img/kg_my_case.png`.
Người đã chọn cho `kg_my_case.png`: **Cái Quang Huy** (đường đi Person → Charge → Crime ← Article, kèm Case → Substance → THRESHOLD → Clause khoản 4).

## Vấn đề gặp phải (không tính điểm)

> 1. **Docker không tải được `neo4j:5`.** `docker pull` đứng ở "Pulling fs layer" hơn 20 phút trên 2 mạng khác nhau, kể cả qua mirror `mirror.gcr.io`, và một lần lỗi `short read: expected 21441810 bytes but got 4759611: unexpected EOF`. curl tải trực tiếp blob từ Docker Hub vẫn được (khoảng 100 KB/s), nên lỗi nằm ở đường proxy nội bộ của Docker Desktop (`http.docker.internal:3128`). Đã chuyển sang Neo4j Aura; code không đổi, chỉ đổi `NEO4J_URI/USER/PASSWORD` trong `.env`.
> 2. **Mạng tới Aura chậm** (ping khoảng 470 ms) gây lỗi `ServiceUnavailable: Unable to retrieve routing information`. Sửa trong `src/graph.py`: tăng `connection_timeout`, `max_transaction_retry_time` của driver và ghi luật bằng `UNWIND` (3 truy vấn thay vì khoảng 32).
> 3. **Gemini free tier giới hạn 100 lần embed/phút**, nên lần benchmark đầu dừng với `429 RESOURCE_EXHAUSTED`. Sửa trong `src/llm.py`: gặp 429 thì chờ đúng `retry in …s` provider trả về rồi thử lại, ghi tổng thời gian chờ ra stderr. Lượt `.hint.txt` chờ 2 lần (59 giây, nằm trong indexing Flat); lượt chính không chờ lần nào.
> 4. **`gemini-2.5-flash-lite` trả 404** cho tài khoản mới, nên chuyển sang `gemini-3.5-flash-lite` qua `.env`; thêm giá vào bảng `src/llm.py`.
> 5. `link_entity` (KG-1) với chỉ `difflib` cutoff 0.8 qua cả 5 test, nhưng thử trên đủ 13 tội thì nối `"sử dụng trái phép chất ma túy"` (vi phạm hành chính) sang `"tổ chức sử dụng…"` (ratio 0.88). Đã thêm bước kiểm tra theo từ (bỏ dấu, mỗi từ có nghĩa phải có từ tương ứng ở phía kia).
