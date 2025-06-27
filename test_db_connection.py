import asyncio
import os
from database import get_pool

async def test_connection():
    print("Attempting to connect to the database...")
    pool = None
    try:
        pool = await get_pool()
        async with pool.acquire() as connection:
            result = await connection.fetchval("SELECT 1")
            if result == 1:
                print("Database connection successful!")
            else:
                print(f"Database connection failed. Unexpected result: {result}")
    except Exception as e:
        print(f"An error occurred during database connection: {e}")
    finally:
        if pool:
            await pool.close()

if __name__ == "__main__":
    # Set a dummy DATABASE_URL for local testing if not already set
    if not os.environ.get("DATABASE_URL"):
        os.environ["DATABASE_URL"] = "postgresql://postgres:postgres@localhost:54322/postgres"
        print("Using dummy DATABASE_URL for local testing.")

    asyncio.run(test_connection())
