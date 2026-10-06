import os
import psycopg
from dotenv import load_dotenv
from langchain_core.messages import HumanMessage, AIMessage
import hashlib
from langchain_chroma import Chroma
from chroma_injection import embedding_model

load_dotenv()
DB = dict(
    host=os.getenv("DB_HOST"),
    dbname=os.getenv("DB_NAME"),
    user=os.getenv("DB_USER"),
    password=os.getenv("DB_PASSWORD"),
    port=os.getenv("DB_PORT"),
)

memory_store = Chroma(
    collection_name="user_memories",
    embedding_function=embedding_model,
    persist_directory="./chroma_db",
)

def init_memory_tables():
    with psycopg.connect(**DB) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS chat_messages (
                id BIGSERIAL PRIMARY KEY,
                user_pass VARCHAR(64) NOT NULL,
                role VARCHAR(10) NOT NULL,
                content TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
            CREATE INDEX IF NOT EXISTS idx_chat_user ON chat_messages (user_pass, id DESC);
        """)

init_memory_tables()

sessions = {}  # in-memory cache: user_id -> list of messages

def load_recent_messages(user_pass: str, limit: int = 12):
    with psycopg.connect(**DB) as conn:
        rows = conn.execute(
            "SELECT role, content FROM chat_messages WHERE user_pass = %s ORDER BY id DESC LIMIT %s;",
            (user_pass, limit),
        ).fetchall()
    rows.reverse()
    return [HumanMessage(content=c) if r == "human" else AIMessage(content=c) for r, c in rows]

def get_session_history(user_pass: str):
    if user_pass not in sessions:
        sessions[user_pass] = load_recent_messages(user_pass)
    return sessions[user_pass]

def trim_history(history, max_turns=6):
    return history[-max_turns * 2:]

def save_exchange(user_pass: str, user_text: str, ai_text: str):
    with psycopg.connect(**DB) as conn:
        conn.execute(
            "INSERT INTO chat_messages (user_pass, role, content) VALUES (%s, 'human', %s), (%s, 'ai', %s);",
            (user_pass, user_text, user_pass, ai_text),
        )

def get_memories(user_pass: str, query: str, k: int = 5):
    docs = memory_store.similarity_search(query, k=k, filter={"user_pass": user_pass})
    return [d.page_content for d in docs]

def add_memories(user_pass: str, facts: list[str]):
    for fact in {f.strip() for f in facts if f and f.strip()}:
        # skip near-duplicates ("owns a TechNest X" vs "has a TechNest X")
        hits = memory_store.similarity_search_with_score(fact, k=1, filter={"user_pass": user_pass})
        if hits and hits[0][1] < 0.25:      # score = distance, lower = more similar
            continue
        fact_id = hashlib.sha256(f"{user_pass}:{fact.lower()}".encode()).hexdigest()
        memory_store.add_texts([fact], metadatas=[{"user_pass": user_pass}], ids=[fact_id])