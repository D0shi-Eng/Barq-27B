# Evaluation evidence / أدلة الاختبارات

[العربية](#العربية) · [English](#english) · [Recorded results](../assets/evaluation.svg)

## English

These are two separate bounded evaluation runs on policy 4.0. Do not combine
their percentages or label them full IFEval/BFCL leaderboard scores.

| Run | Cases | Result | Thinking/output cap | Timing |
|---|---:|---|---|---|
| Synthetic smoke | 12 | 11/12, 91.67% | 128/512 tokens | 32.328 s summed; median 2.1165 s |
| Official-data subsets | 16 of 20 selected | IFEval 8/8 prompts and 17/17 constraints; BFCL 7/8 | Default, 512/2048 tokens | 205.391 s window; median 9.617 s |

The latter ran with temperature 0 and seed 42, one sequential request, a 90-second
request timeout, no automatic retries, no real tool execution and no extra model
loading. It was bounded to 600 seconds and stopped admitting requests at 500.
The resource gate stopped it after 16 cases. A later read-only observation found
3.70 GiB available RAM; the rejected gate itself did not save the failed reading.
Four selected cases remained NOT_RUN, including BFCL irrelevance. There were no
transport errors, scorer errors or token-limit truncations in the completed cases.

IFEval uses the unmodified Google strict/loose checker. BFCL uses the unmodified
Python AST checker and official API schema conversion; the adapter supplies only
the model-name formatting flag. Selected source revisions and hashes are in
`instruction-and-tools/source-manifest.json`. Selection seed: 20261006. The
original prompts and all constraints were fixed before inference. Reference
answers were used only for scoring and were not sent to the model.

**Retained failures:**

- BFCL `multiple_93`: correct hotel-booking function; `location` was
  `Marriott hotel in New York`, while the reference accepts `New York`,
  `New York, NY` or `NYC`. Other booking arguments were correct. No manual override.
- Smoke `missing-city`: asked for exact `في أي مدينة؟`; the model produced
  `في أي مدينة تريد معرفة الطقس؟`. Semantically appropriate, but the exact-match
  failure is preserved.

The combined official subset reports 35,565 prompt tokens, 7,308 completion tokens
and 194.813 seconds summed request time. 37.513 completion tokens per wall second
includes prompt processing and reasoning; it is not decoder-only throughput.
First visible answer/tool delta median: 7.633 s; longest request: 42.547 s.
Smoke's Max case had only a 128-token thinking allowance, not the normal Max cap.
No baseline A/B, full-context, sustained-load or full six-mode comparison was run.

See [export and offline reproduction instructions](EXPORT-NOTES.md),
[raw official-subset responses](instruction-and-tools/responses.jsonl),
[raw smoke responses](smoke/responses.jsonl), and [file hashes](../SHA256SUMS.json).

## العربية

هذه جولتان مستقلتان بسياسة 4.0؛ لا تجمع نسبتيهما ولا تعرضهما كدرجات benchmark
كامل أوترتيب رسمي.

| الجولة | العدد | النتيجة | حدود التفكير والإخراج | الزمن |
|---|---:|---|---|---|
| الفحص المخصص | 12 | 11/12؛ 91.67% | 128/512 توكن | مجموع32.328 ثانية؛ وسيط2.1165 |
| عينات البيانات الرسمية | 16 من20 مخططة | IFEval: 8/8 طلبات و17/17 قيدًا؛ BFCL: 7/8 | Default؛ 512/2048 | نافذة205.391 ثانية؛ وسيط9.617 |

اختيرت الحالات قبل التنفيذ ببذرة20261006. استخدمنا مصحح IFEval الأصلي في
strict وloose ومصحح BFCL AST الأصلي مع محول مخططات الأدوات الرسمي. لم تُرسل
الإجابات المرجعية للنموذج. الإصدارات والبصمات محفوظة في source-manifest.json.
أُعيد التصحيح على CPU دون الاتصال بالسيرفر وتطابقت الأحكام الستة عشر.

كانت الطلبات متتابعة بعامل واحد، دون إعادة محاولة أوتحميل نموذج إضافي أوتنفيذ
أدوات فعلية. سقف الجولة600 ثانية ومهلة الطلب90 ثانية. توقفت بعد16 حالة عند
فحص الموارد؛ قراءة لاحقة وجدت RAM متاحة3.70 GiB. الفحص المرفوض نفسه لم يحفظ
قيمة القراءة. بقيت أربع حالات غير منفذة، ومنها الامتناع عن أداة غير مناسبة.
لا توجد أخطاء نقل أوتصحيح أوإجابات مقتطعة في الحالات المنفذة.

حُفظ إخفاق BFCL لأن location احتوى اسم الفندق مع المدينة بينما المرجع يقبل
المدينة وحدها. وحُفظ إخفاق الفحص المخصص لاختلاف صيغة سؤال الاستيضاح حرفيًا.
لا يوجد تعديل يدوي للدرجات. معدل37.513 توكن/ثانية هو توكنات التوليد مقسومة
على زمن الطلبات، ويشمل معالجة المدخل والتفكير؛ ليس سرعة decoder منفردًا.
حالة Max الأولية بميزانية128 لا تقيس Max الكامل. لا توجد مقارنة A/B أوفحص ضغط
أوقياس سياق ممتلئ بالكامل.

[تعليمات إعادة التصحيح](EXPORT-NOTES.md) · [الأجوبة الخام](instruction-and-tools/responses.jsonl)
· [خريطة التطوير](../docs/ROADMAP.md)

## Sources

[Google IFEval](https://github.com/google-research/google-research/tree/e49bbfe381c9c0e564b937f1c4e163a2273c65cc/instruction_following_eval)
· [Berkeley BFCL](https://github.com/ShishirPatil/gorilla/tree/6ea57973c7a6097fd7c5915698c54c17c5b1b6c8/berkeley-function-call-leaderboard)
