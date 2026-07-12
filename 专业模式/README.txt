专业模式

定位：
专业模式是在 flash 模式外再套一层多智能体协作互检。
它不替代原 codes 流水线，而是把原流水线当作候选生成单元，多跑几条独立候选路线，再由 Selector 选择最可靠结果。

运行：
python 专业模式\professional_pipeline.py

只查看配置：
python 专业模式\professional_pipeline.py --show-options

常用参数：
--input examples\input.md
--output-dir outputs_professional
--candidate-count 2
--max-candidate-attempts 4
--selector-min-score 95
--duplicate-similarity-threshold 0.88

工作流程：
1. Professional Reader
   读取输入，整理题面，拆成单题。

2. Candidate Pipeline A/B/...
   每一道题独立跑多条候选路线。
   每条候选路线内部仍是 flash 的完整流程：
   Decomposer -> Reviewer -> Generator -> QC。

3. Professional Selector
   读取每个候选的 QC 结果。
   优先选择 QC 通过、分数高、major 问题少、fallback 少的版本。
   若候选与同题其他候选或前面已选题目过于相似，会降低权重，尽量避免最终结果出现大量重复题。

4. Final Composer
   按 Reader 原顺序合并每道题被选中的候选结果。
   输出最终 questions、answer key、QC 总结和 VSCode 可读预览。

主要输出：
outputs_professional\generated_questions_final.md
outputs_professional\answer_key_final.md
outputs_professional\qc_final.json
outputs_professional\vscode_preview.md
outputs_professional\professional_summary.json

优点：
正确率更高。
单道题失败时更容易定位。
候选之间形成竞争，能减少偶然错误。
保留 flash 模式全部能力，包括 OCR 后输入、缓存、超时警告、结果库 fallback。

代价：
耗时和 token 使用量约为 flash 模式的 candidate_count 倍。
若检测到和前文过度重复，专业模式可能自动补跑候选，最多到 max_candidate_attempts。
如果 candidate_count 设得过高，UI 等待时间会明显增长。
