import io

def rep(path, old, new):
    s = io.open(path, encoding="utf-8").read()
    n = s.count(old)
    assert n >= 1, (path, old[:70], n)
    s = s.replace(old, new)
    io.open(path, "w", encoding="utf-8").write(s)
    print(f"{path}: replaced {n}")

CREDIT = "AND COALESCE(account_type,'debit') <> 'credit' "

rep("mock_bank/main.py",
    '"SELECT id, name, balance FROM accounts WHERE user_id=%s "\n'
    '                                "ORDER BY id ASC LIMIT 1 FOR UPDATE",',
    '"SELECT id, name, balance FROM accounts WHERE user_id=%s "\n'
    '                                "AND COALESCE(account_type,\'debit\') <> \'credit\' "\n'
    '                                "ORDER BY id ASC LIMIT 1 FOR UPDATE",')

rep("mock_bank/main.py",
    '"SELECT id, name, balance FROM accounts WHERE user_id=%s "\n'
    '                        "ORDER BY id ASC LIMIT 1 FOR UPDATE",',
    '"SELECT id, name, balance FROM accounts WHERE user_id=%s "\n'
    '                        "AND COALESCE(account_type,\'debit\') <> \'credit\' "\n'
    '                        "ORDER BY id ASC LIMIT 1 FOR UPDATE",')

rep("mock_bank/main.py",
    '"SELECT id, balance FROM accounts WHERE user_id=%s "\n'
    '                    "ORDER BY id ASC LIMIT 1 FOR UPDATE",',
    '"SELECT id, balance FROM accounts WHERE user_id=%s "\n'
    '                    "AND COALESCE(account_type,\'debit\') <> \'credit\' "\n'
    '                    "ORDER BY id ASC LIMIT 1 FOR UPDATE",')

rep("mock_bank/main.py",
    '"SELECT id, name, balance FROM accounts "\n'
    '                        "WHERE user_id=%s ORDER BY id ASC LIMIT 1 FOR UPDATE",',
    '"SELECT id, name, balance FROM accounts "\n'
    '                        "WHERE user_id=%s AND COALESCE(account_type,\'debit\') <> \'credit\' "\n'
    '                        "ORDER BY id ASC LIMIT 1 FOR UPDATE",')

rep("run_server.py",
    '"SELECT COALESCE(SUM(balance), 0) AS total FROM accounts "\n'
    '                    "WHERE user_id=%s", (uid,)',
    '"SELECT COALESCE(SUM(balance), 0) AS total FROM accounts "\n'
    '                    "WHERE user_id=%s AND COALESCE(account_type,\'debit\') <> \'credit\'", (uid,)')

rep("run_server.py",
    '"COALESCE((SELECT SUM(balance) FROM accounts a WHERE a.user_id=u.id), 0) AS assets "',
    '"COALESCE((SELECT SUM(balance) FROM accounts a WHERE a.user_id=u.id "\n'
    '                          "AND COALESCE(a.account_type,\'debit\') <> \'credit\'), 0) AS assets "')

rep("utils/deduction_engine.py",
    '"SELECT id, name, balance FROM accounts WHERE user_id=%s ORDER BY id ASC FOR UPDATE"',
    '"SELECT id, name, balance FROM accounts WHERE user_id=%s "\n'
    '    "AND COALESCE(account_type,\'debit\') <> \'credit\' ORDER BY id ASC FOR UPDATE"')

rep("profile_db.py",
    '"SELECT name, balance FROM accounts WHERE user_id=%s ORDER BY id", (user_id,)',
    '"SELECT name, balance FROM accounts WHERE user_id=%s "\n'
    '                "AND COALESCE(account_type,\'debit\') <> \'credit\' ORDER BY id", (user_id,)')
print("DONE")
