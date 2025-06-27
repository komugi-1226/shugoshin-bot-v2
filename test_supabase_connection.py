#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Supabaseローカル環境への接続テスト
直接PostgreSQLにasyncpgで接続して動作確認を行う
"""

import asyncio
import os
from dotenv import load_dotenv
import database as db

async def test_supabase_connection():
    """Supabaseローカル環境への接続をテストする"""
    print("🔍 Supabaseローカル環境への接続テストを開始...")
    
    try:
        # 環境変数を読み込み
        load_dotenv()
        db_url = os.getenv('DATABASE_URL')
        print(f"📍 接続先: {db_url}")
        
        # データベース接続プールを取得
        pool = await db.get_pool()
        print("✅ データベース接続プール作成成功")
        
        # 接続テスト
        async with pool.acquire() as connection:
            # PostgreSQLのバージョンを確認
            version = await connection.fetchval('SELECT version()')
            print(f"📊 PostgreSQLバージョン: {version}")
            
            # 現在の時刻を取得
            current_time = await connection.fetchval('SELECT NOW()')
            print(f"⏰ データベース現在時刻: {current_time}")
            
            # テーブル作成テスト（守護神ボット用）
            print("\n🛠️  守護神ボット用テーブルの作成テスト...")
            await db.init_shugoshin_db()
            print("✅ 守護神ボット用テーブル作成成功")
            
            # 作成されたテーブルを確認
            tables = await connection.fetch("""
                SELECT table_name 
                FROM information_schema.tables 
                WHERE table_schema = 'public' 
                AND table_type = 'BASE TABLE'
                ORDER BY table_name
            """)
            
            print(f"\n📋 作成されたテーブル一覧:")
            for table in tables:
                print(f"  - {table['table_name']}")
        
        # プールを閉じる
        await pool.close()
        print("\n🎉 すべてのテストが成功しました！Supabaseローカル環境への移行が完了です。")
        return True
        
    except Exception as e:
        print(f"❌ エラーが発生しました: {e}")
        print(f"エラー詳細: {type(e).__name__}")
        return False

if __name__ == "__main__":
    # 非同期関数を実行
    success = asyncio.run(test_supabase_connection())
    if success:
        print("\n✨ Supabaseローカル環境の準備完了！main.pyを実行してBotを起動できます。")
    else:
        print("\n⚠️  接続に問題があります。設定を確認してください。")
