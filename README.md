<p align="center"><strong>BARQ 27B · برق</strong></p>
<h1 align="center">Local reasoning. Clear controls. Verifiable evidence.</h1>
<p align="center">تشغيل محلي · ستة أنماط · أدوات وذاكرة محددة النطاق · نتائج قابلة للتحقق</p>
<p align="center">
  <a href="docs/README.ar.md"><img src="assets/language-ar.svg" alt="اقرأ التوثيق بالعربية" width="160" height="42"></a>
  &nbsp;
  <a href="docs/README.en.md"><img src="assets/language-en.svg" alt="Read the English documentation" width="160" height="42"></a>
</p>

![Barq architecture](assets/architecture.svg)

**Barq 27B** is a local derivative serving distribution with an original operating
policy, an embedded GGUF template, six bounded reasoning modes, a sequential
Chat Completions gateway and project-scoped external memory. Numerical tensor
weights are unchanged; this release does not claim new weight training.

**برق 27B** حزمة تشغيل محلية مشتقة، تشمل سياسة تشغيل أصلية وقالبًا مضمّنًا وستة
أنماط تفكير بحدود واضحة وبوابة طلبات متتابعة وذاكرة خارجية حسب المشروع.
الأوزان العددية لم تُدرَّب من جديد. [الأصل والتراخيص / Provenance](legal/NOTICE.txt).

## Recorded results · النتائج المسجلة

| Evaluation / التقييم | Result / النتيجة | Scope / النطاق |
|---|---:|---|
| IFEval strict | **8/8 prompts; 17/17 constraints** | Fixed small subset / عينة صغيرة ثابتة |
| BFCL v4 | **7/8 cases (87.5%)** | Basic native function calling / استدعاء أدوات أساسي |
| Synthetic smoke suite | **11/12 cases (91.67%)** | Separate custom checks / فحوص مخصصة منفصلة |

These are sample results, not full benchmark scores or leaderboard rankings.
One BFCL argument-value failure is retained. Four planned official-data cases
were not run after a resource gate stopped the session.

هذه نتائج عينات وليست درجات الاختبارات الكاملة. حُفظ إخفاق BFCL كما هو، ولم
تُنفّذ أربع حالات مخططة بعد توقف الجولة عند فحص الموارد.

![Measured evaluation](assets/evaluation.svg)

**Start here:** [العربية](docs/README.ar.md) · [English](docs/README.en.md) ·
[Evaluation evidence](evaluations/README.md) · [Future development / التطوير القادم](docs/ROADMAP.md)

This source repository includes the final policy, gateway, integrations and
evaluation evidence. GGUF weights and runtime binaries are obtained separately.
The documented client endpoint is `http://127.0.0.1:8081/v1`.

إن شاء الله سيستمر تطوير المشروع، مع نشر الأدلة والحدود الفعلية لكل تحسين.
Future development will, God willing, continue with evidence for each improvement.
