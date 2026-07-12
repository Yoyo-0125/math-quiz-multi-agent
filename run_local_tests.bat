@echo off
cd /d "%~dp0"
python tests\local_validation_tests.py || exit /b 1
python tests\input_coverage_evaluator.py || exit /b 1
python tests\sample_quality_evaluator.py || exit /b 1
python tests\curriculum_coverage_evaluator.py || exit /b 1
python tests\compile_all.py || exit /b 1
