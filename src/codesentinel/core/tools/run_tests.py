"""run_tests tool. Must execute via core.sandbox's restricted subprocess
(no network, confined working directory, timeout) — never a raw subprocess
call, since the test target and repo content are attacker-influenceable.
"""
