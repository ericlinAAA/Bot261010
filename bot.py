import telebot
import os
import psycopg2
import threading
from psycopg2.extras import DictCursor
from http.server import BaseHTTPRequestHandler, HTTPServer
from telebot.types import ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove

# ==========================================
# 1. 環境變數設定
# ==========================================
TOKEN = os.getenv('TELEGRAM_TOKEN', '若在本地測試請填入Token')
DATABASE_URL = os.getenv('DATABASE_URL', '若在本地測試請填入Neon連線字串')

bot = telebot.TeleBot(TOKEN)

# --- 雙維度選項清單 ---
DIM1_OPTIONS = [
    "治療目標已達成", "明顯改善，尚未完全達標", "部分改善",
    "成效不明顯／未改善", "症狀惡化", "無法評估"
]

DIM2_OPTIONS = [
    "治療目標已達成", "症狀改善選擇自主維持", "成效不如預期",
    "個人因素（私事）", "交通問題（搬家）", "時間安排衝突",
    "經濟因素", "身體狀況因素", "轉至其他院所或機構",
    "個案或家屬意願降低", "未知／失聯"
]

# ==========================================
# 2. 資料庫功能區 (連線 Neon PostgreSQL)
# ==========================================
def get_db_connection():
    return psycopg2.connect(DATABASE_URL, sslmode='require')

def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS lost_cases (
            id SERIAL PRIMARY KEY,
            case_name VARCHAR(255) NOT NULL,
            reason VARCHAR(255) NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    # 自動為現有資料表加入雙維度欄位 (舊資料預設為"無")
    cursor.execute("ALTER TABLE lost_cases ADD COLUMN IF NOT EXISTS dim1 VARCHAR(255) DEFAULT '無'")
    cursor.execute("ALTER TABLE lost_cases ADD COLUMN IF NOT EXISTS dim2 VARCHAR(255) DEFAULT '無'")
    conn.commit()
    cursor.close()
    conn.close()

# ==========================================
# 3. 機器人介面與選單生成器
# ==========================================
def get_main_menu():
    markup = ReplyKeyboardMarkup(resize_keyboard=True)
    markup.row(KeyboardButton('➕ 新增個案'), KeyboardButton('🔍 查詢個案'))
    markup.row(KeyboardButton('✏️ 修改個案'), KeyboardButton('❌ 刪除個案'))
    markup.row(KeyboardButton('📊 查看統計'))
    return markup

# 通用選項按鈕生成器 (兩兩一排)
def generate_menu_from_list(options_list):
    markup = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    for i in range(0, len(options_list), 2):
        if i + 1 < len(options_list):
            markup.row(KeyboardButton(options_list[i]), KeyboardButton(options_list[i+1]))
        else:
            markup.row(KeyboardButton(options_list[i]))
    markup.row(KeyboardButton('取消操作'))
    return markup

def get_dim1_menu():
    return generate_menu_from_list(DIM1_OPTIONS)

def get_dim2_menu():
    return generate_menu_from_list(DIM2_OPTIONS)

def get_cancel_menu():
    return ReplyKeyboardMarkup(resize_keyboard=True).add(KeyboardButton('取消操作'))

@bot.message_handler(commands=['start'])
def send_welcome(message):
    bot.reply_to(message, "歡迎使用「個案流失紀錄系統」！\n請透過下方選單進行操作：", reply_markup=get_main_menu())

# ==================== 新增功能 ====================
@bot.message_handler(func=lambda message: message.text == '➕ 新增個案')
def add_case_start(message):
    msg = bot.reply_to(message, "請輸入「流失個案的名稱」：\n(點擊下方按鈕可取消)", reply_markup=get_cancel_menu())
    bot.register_next_step_handler(msg, process_case_name_step)

def process_case_name_step(message):
    if message.text == '取消操作':
        bot.reply_to(message, "已取消新增。", reply_markup=get_main_menu())
        return
    case_name = message.text
    msg = bot.reply_to(message, f"個案：【{case_name}】\n請選擇【第一維度：治療成效】：", reply_markup=get_dim1_menu())
    bot.register_next_step_handler(msg, process_dim1_step, case_name)

def process_dim1_step(message, case_name):
    if message.text == '取消操作':
        bot.reply_to(message, "已取消新增。", reply_markup=get_main_menu())
        return
    dim1 = message.text
    msg = bot.reply_to(message, f"已選擇：{dim1}\n接著，請選擇【第二維度：流失原因】：", reply_markup=get_dim2_menu())
    bot.register_next_step_handler(msg, process_dim2_step, case_name, dim1)

def process_dim2_step(message, case_name, dim1):
    if message.text == '取消操作':
        bot.reply_to(message, "已取消新增。", reply_markup=get_main_menu())
        return
    dim2 = message.text
    # 將雙維度組合成一個字串存入原本的 reason 欄位，方便後續關鍵字搜尋
    combined_reason = f"{dim1} / {dim2}"
    
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO lost_cases (case_name, reason, dim1, dim2) VALUES (%s, %s, %s, %s)", 
        (case_name, combined_reason, dim1, dim2)
    )
    conn.commit()
    cursor.close()
    conn.close()
    
    bot.reply_to(message, f"✅ 紀錄成功！\n\n👤 個案：{case_name}\n🔹 治療成效：{dim1}\n🔹 流失原因：{dim2}", reply_markup=get_main_menu())

# ==================== 查詢功能 ====================
@bot.message_handler(func=lambda message: message.text == '🔍 查詢個案')
def search_case_start(message):
    msg = bot.reply_to(message, "請輸入要查詢的「個案名稱」或「流失原因關鍵字」：", reply_markup=get_cancel_menu())
    bot.register_next_step_handler(msg, process_search_step)

def process_search_step(message):
    if message.text == '取消操作':
        bot.reply_to(message, "已取消查詢。", reply_markup=get_main_menu())
        return
    keyword = message.text
    
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=DictCursor)
    # 同時搜尋名稱或原因
    cursor.execute("SELECT id, case_name, reason, created_at::date FROM lost_cases WHERE case_name LIKE %s OR reason LIKE %s", (f'%{keyword}%', f'%{keyword}%'))
    results = cursor.fetchall()
    cursor.close()
    conn.close()
    
    if results:
        reply = f"🔍 找到關於「{keyword}」的紀錄：\n\n"
        for row in results:
            reply += f"🔹 [ID: {row['id']}] {row['case_name']} \n📝 原因：{row['reason']} ({row['created_at']})\n\n"
        bot.reply_to(message, reply, reply_markup=get_main_menu())
    else:
        bot.reply_to(message, "找不到相關個案。", reply_markup=get_main_menu())

# ==================== 刪除功能 ====================
@bot.message_handler(func=lambda message: message.text == '❌ 刪除個案')
def delete_start(message):
    msg = bot.reply_to(message, "請先輸入要刪除的「個案名稱」關鍵字：", reply_markup=get_cancel_menu())
    bot.register_next_step_handler(msg, delete_search)

def delete_search(message):
    if message.text == '取消操作':
        bot.reply_to(message, "已取消刪除。", reply_markup=get_main_menu())
        return
    keyword = message.text
    
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=DictCursor)
    cursor.execute("SELECT id, case_name, reason FROM lost_cases WHERE case_name LIKE %s", (f'%{keyword}%',))
    results = cursor.fetchall()
    cursor.close()
    conn.close()
    
    if results:
        reply = "找到以下資料：\n\n"
        for row in results:
            reply += f"🔹 [ID: {row['id']}] {row['case_name']} - {row['reason']}\n"
        msg = bot.reply_to(message, reply + "\n⚠️ 請輸入您要刪除的【數字 ID】：", reply_markup=get_cancel_menu())
        bot.register_next_step_handler(msg, delete_confirm)
    else:
        bot.reply_to(message, "找不到相關個案。", reply_markup=get_main_menu())

def delete_confirm(message):
    if message.text == '取消操作':
        bot.reply_to(message, "已取消刪除。", reply_markup=get_main_menu())
        return
    if not message.text.isdigit():
        bot.reply_to(message, "錯誤：ID 必須是數字，操作已取消。", reply_markup=get_main_menu())
        return
    
    target_id = int(message.text)
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM lost_cases WHERE id = %s", (target_id,))
    deleted_count = cursor.rowcount
    conn.commit()
    cursor.close()
    conn.close()
    
    if deleted_count > 0:
        bot.reply_to(message, f"🗑️ 成功刪除 ID [{target_id}] 的紀錄。", reply_markup=get_main_menu())
    else:
        bot.reply_to(message, f"找不到 ID [{target_id}] 的資料。", reply_markup=get_main_menu())

# ==================== 修改功能 ====================
@bot.message_handler(func=lambda message: message.text == '✏️ 修改個案')
def edit_start(message):
    msg = bot.reply_to(message, "請先輸入要修改的「個案名稱」關鍵字：", reply_markup=get_cancel_menu())
    bot.register_next_step_handler(msg, edit_search)

def edit_search(message):
    if message.text == '取消操作':
        bot.reply_to(message, "已取消修改。", reply_markup=get_main_menu())
        return
    keyword = message.text
    
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=DictCursor)
    cursor.execute("SELECT id, case_name, reason FROM lost_cases WHERE case_name LIKE %s", (f'%{keyword}%',))
    results = cursor.fetchall()
    cursor.close()
    conn.close()
    
    if results:
        reply = "找到以下資料：\n\n"
        for row in results:
            reply += f"🔹 [ID: {row['id']}] {row['case_name']} - {row['reason']}\n"
        msg = bot.reply_to(message, reply + "\n✏️ 請輸入您要修改的【數字 ID】：", reply_markup=get_cancel_menu())
        bot.register_next_step_handler(msg, edit_get_id)
    else:
        bot.reply_to(message, "找不到相關個案。", reply_markup=get_main_menu())

def edit_get_id(message):
    if message.text == '取消操作':
        bot.reply_to(message, "已取消修改。", reply_markup=get_main_menu())
        return
    if not message.text.isdigit():
        bot.reply_to(message, "錯誤：ID 必須是數字，操作已取消。", reply_markup=get_main_menu())
        return
    
    target_id = int(message.text)
    msg = bot.reply_to(message, f"正在修改 ID [{target_id}] 的資料。\n請輸入「新的個案名稱」：\n(若不修改名稱，請輸入 0)", reply_markup=get_cancel_menu())
    bot.register_next_step_handler(msg, edit_get_name, target_id)

def edit_get_name(message, target_id):
    if message.text == '取消操作':
        bot.reply_to(message, "已取消修改。", reply_markup=get_main_menu())
        return
    
    new_name = message.text
    msg = bot.reply_to(message, "請選擇新的【第一維度：治療成效】：", reply_markup=get_dim1_menu())
    bot.register_next_step_handler(msg, edit_get_dim1, target_id, new_name)

def edit_get_dim1(message, target_id, new_name):
    if message.text == '取消操作':
        bot.reply_to(message, "已取消修改。", reply_markup=get_main_menu())
        return
    dim1 = message.text
    msg = bot.reply_to(message, f"已選擇：{dim1}\n請選擇新的【第二維度：流失原因】：", reply_markup=get_dim2_menu())
    bot.register_next_step_handler(msg, edit_save, target_id, new_name, dim1)

def edit_save(message, target_id, new_name, dim1):
    if message.text == '取消操作':
        bot.reply_to(message, "已取消修改。", reply_markup=get_main_menu())
        return
    
    dim2 = message.text
    combined_reason = f"{dim1} / {dim2}"
    
    conn = get_db_connection()
    cursor = conn.cursor()
    
    if new_name == '0':
        cursor.execute("UPDATE lost_cases SET reason = %s, dim1 = %s, dim2 = %s WHERE id = %s", (combined_reason, dim1, dim2, target_id))
    else:
        cursor.execute("UPDATE lost_cases SET case_name = %s, reason = %s, dim1 = %s, dim2 = %s WHERE id = %s", (new_name, combined_reason, dim1, dim2, target_id))
        
    conn.commit()
    cursor.close()
    conn.close()
    bot.reply_to(message, f"✅ 修改成功！", reply_markup=get_main_menu())

# ==================== 統計功能 ====================
@bot.message_handler(func=lambda message: message.text == '📊 查看統計')
def view_stats(message):
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=DictCursor)
    
    # 統計第一維度
    cursor.execute("SELECT dim1, COUNT(*) as count FROM lost_cases WHERE dim1 != '無' GROUP BY dim1 ORDER BY count DESC")
    dim1_results = cursor.fetchall()
    
    # 統計第二維度
    cursor.execute("SELECT dim2, COUNT(*) as count FROM lost_cases WHERE dim2 != '無' GROUP BY dim2 ORDER BY count DESC")
    dim2_results = cursor.fetchall()
    
    cursor.close()
    conn.close()
    
    reply = "📊 【流失個案統計報告】\n\n"
    
    if dim1_results or dim2_results:
        reply += "📈 第一維度：治療成效\n"
        for index, row in enumerate(dim1_results, 1):
            reply += f"{index}. {row['dim1']}: {row['count']} 件\n"
            
        reply += "\n📉 第二維度：流失原因\n"
        for index, row in enumerate(dim2_results, 1):
            reply += f"{index}. {row['dim2']}: {row['count']} 件\n"
            
        reply += "\n💡 提示：若想查看特定原因的個案名單，請點擊「🔍 查詢個案」，直接輸入原因關鍵字（例如：經濟因素）即可搜尋出所有相關個案！"
        bot.reply_to(message, reply, reply_markup=get_main_menu())
    else:
        bot.reply_to(message, "目前還沒有雙維度的流失紀錄資料。", reply_markup=get_main_menu())

@bot.message_handler(func=lambda message: True)
def echo_all(message):
    bot.reply_to(message, "請使用下方選單進行操作 👇", reply_markup=get_main_menu())

# ==========================================
# 4. Render 專用的背景網頁伺服器
# ==========================================
class DummyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(b"Bot is running!")

def run_dummy_server():
    port = int(os.environ.get('PORT', 10000))
    server = HTTPServer(('0.0.0.0', port), DummyHandler)
    server.serve_forever()

if __name__ == '__main__':
    print("正在連接資料庫並啟動機器人...")
    init_db()
    threading.Thread(target=run_dummy_server, daemon=True).start()
    bot.infinity_polling(timeout=10, long_polling_timeout=5)