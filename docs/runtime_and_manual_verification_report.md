Runtime And Manual Verification Report

Scope:
9 coverage rounds, 41 generated questions.
Each round ran the full project pipeline with QC enabled.
QC threshold: 95.
Per-round runtime limit: 360 seconds.
Manual verification was performed after QC by reading generated_questions_final.md and answer_key_final.md.

Summary:
type coverage: 36/36 = 100%
difficulty coverage: 3/3 = 100%
manual correctness: 41/41 = 100%
wrong answers: 0
manual error rate: 0%
target correctness: >=95%
all checked rounds within 360 seconds: yes

Round Results:
1. round_01_junior_algebra_core
   questions: 5
   runtime: 155.56 seconds
   QC score: 95
   manual result: 5/5 correct

2. round_02_junior_equations_ratio
   questions: 5
   runtime: 62.86 seconds
   QC score: 95
   manual result: 5/5 correct

3. round_03_junior_functions_data
   questions: 5
   runtime: 45.58 seconds
   QC score: 100
   manual result: 5/5 correct

4. round_04_high_functions
   questions: 5
   runtime: 102.08 seconds
   QC score: 100
   manual result: 5/5 correct

5. round_05_high_trig_inequality
   questions: 4
   runtime: 67.06 seconds
   QC score: 95
   manual result: 4/4 correct

6. round_06_high_sequences_binomial
   questions: 4
   runtime: 38.66 seconds
   QC score: 100
   manual result: 4/4 correct

7. round_07_high_vectors_complex
   questions: 4
   runtime: 43.92 seconds
   QC score: 100
   manual result: 4/4 correct

8. round_08_high_analytic_geometry
   questions: 4
   runtime: 65.78 seconds
   QC score: 100
   manual result: 4/4 correct

9. round_09_high_probability_calculus
   questions: 5
   runtime: 81.86 seconds
   QC score: 100
   manual result: 5/5 correct

Notes:
The Generator can still produce a wrong item count on some mixed worksheets.
To reduce this risk, an itemwise fallback was added: when match_source count checking fails, the system generates one item per source item and merges them with stable numbering.
The validator now counts numbered calculation/simplification/factorization questions even when the line has no equality or inequality symbol.
The config reader now accepts UTF-8 files with BOM.

