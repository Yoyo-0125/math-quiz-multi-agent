@echo off
cd /d "%~dp0"
python tests\local_validation_tests.py
python -m py_compile codes\validators.py codes\decomposer_agent.py codes\generator_agent.py codes\qc_agent.py codes\run_pipeline.py tests\local_validation_tests.py

