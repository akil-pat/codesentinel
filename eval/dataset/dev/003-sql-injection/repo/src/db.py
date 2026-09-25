def find_user(conn, username):
    cursor = conn.execute(f"SELECT * FROM users WHERE username = '{username}'")
    return cursor.fetchone()
