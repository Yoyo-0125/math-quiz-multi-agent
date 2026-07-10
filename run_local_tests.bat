@echo off
cd /d "%~dp0"
python tests\local_validation_tests.py || exit /b 1
python tests\input_coverage_evaluator.py || exit /b 1
python tests\sample_quality_evaluator.py || exit /b 1
python tests\curriculum_coverage_evaluator.py || exit /b 1
python -m py_compile codes\validators.py codes\decomposer_agent.py codes\generator_agent.py codes\qc_agent.py codes\run_pipeline.py tests\local_validation_tests.py tests\input_coverage_evaluator.py tests\sample_quality_evaluator.py tests\curriculum_coverage_evaluator.py tests\project_runtime_benchmark.py || exit /b 1
