flash模式

定位：
原 codes/run_pipeline.py 的现有流水线被封装为 flash 模式。
它强调速度、可用性和单线闭环：Reader 分题后，每道题依次经过 Decomposer、Reviewer、Generator、QC，最后合并输出。

运行：
python flash模式\run_flash.py

等价命令：
python codes\run_pipeline.py

常用输出：
outputs\generated_questions_final.md
outputs\answer_key_final.md
outputs\qc_final.json
outputs\vscode_preview.md

适用场景：
快速测试、课堂展示、UI 调试、较短输入的批量变式生成。

和专业模式的区别：
flash 模式每道题默认只保留一条生成路线；专业模式会为每道题生成多个候选，并由 QC 与 Selector 共同择优。
