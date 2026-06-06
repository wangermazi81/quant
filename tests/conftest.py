# pytest 收集配置
# baseline_test.py 与 test_ai_api.py 是「对运行中的 5001 服务」的独立冒烟脚本
# （直接用 python tests/xxx.py 运行，依赖 ADMIN_PASSWORD / 实时服务），
# 它们在模块级 sys.exit/SystemExit，会打断 pytest 收集，故从 pytest 中排除。
collect_ignore = ["baseline_test.py", "test_ai_api.py"]
