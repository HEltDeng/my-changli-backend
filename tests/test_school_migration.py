import sqlite3
import unittest
import school_bindings

class SchoolMigrationTest(unittest.TestCase):
    def test_nullable_account_migration_preserves_rows_constraints_and_repeats(self):
        db=sqlite3.connect(":memory:")
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("CREATE TABLE users(id TEXT PRIMARY KEY)")
        db.execute("CREATE TABLE installations(installation_id TEXT PRIMARY KEY)")
        db.execute("INSERT INTO users VALUES('synthetic-account')")
        db.execute("INSERT INTO installations VALUES('synthetic-install')")
        school_bindings.migrate(db)
        sql=db.execute("SELECT sql FROM sqlite_master WHERE name='school_bindings'").fetchone()[0]
        db.execute("DROP TABLE school_bindings")
        db.execute(sql.replace('user_id TEXT REFERENCES','user_id TEXT NOT NULL REFERENCES'))
        row=('binding','synthetic-install','synthetic-account','csust','00123456','ACTIVE','CLIENT_SCHOOL_LOGIN',1,100,101,None,'synthetic-event')
        db.execute("INSERT INTO school_bindings VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",row)
        db.commit()
        before=list(db.execute("SELECT * FROM school_bindings"))
        for _ in range(2):
            with db: school_bindings.migrate(db)
            self.assertEqual(before,list(db.execute("SELECT * FROM school_bindings")))
            self.assertEqual([],list(db.execute("PRAGMA foreign_key_check")))
            self.assertEqual('ok',db.execute("PRAGMA integrity_check").fetchone()[0])
        db.execute("INSERT INTO installations VALUES('anonymous-install')")
        db.execute("INSERT INTO school_bindings VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",('anon','anonymous-install',None,*row[3:]))
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute("INSERT INTO school_bindings VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",('dup','anonymous-install',None,*row[3:]))
        db.close()
