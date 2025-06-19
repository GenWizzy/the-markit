import psycopg2

def create_schema():
    conn = psycopg2.connect(
        dbname="themarkit",
        user="postgres",
        password="Kendrickiii@911",
        host="localhost"
    )
    cursor = conn.cursor()

    cursor.execute('''
    CREATE TABLE IF NOT EXISTS products (
        id SERIAL PRIMARY KEY,
        category TEXT,
        location TEXT,
        price_range TEXT,
        description TEXT,
        photo_file_id TEXT,
        chat_id BIGINT,
        timestamp BIGINT
    )
    ''')

    conn.commit()
    cursor.close()
    conn.close()

    print("Database schema created successfully!")

if __name__ == "__main__":
    create_schema()