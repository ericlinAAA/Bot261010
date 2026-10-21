import telebot
import os
import psycopg2
import threading
from psycopg2.extras import DictCursor
from http.server import BaseHTTPRequestHandler, HTTPServer
from telebot.types import (
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove,
    InlineKeyboardMarkup, InlineKeyboardButton
)

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

# 暫存使用者在「分類瀏覽與批次修改」中的操作狀態
user_batch_state = {}

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
    markup.row(KeyboardButton('📂 依分類瀏覽與批次修改'))
    markup.row(KeyboardButton('✏️ 單筆修改'), KeyboardButton('❌ 刪除個案'))
    markup.row(KeyboardButton('📊 查看統計'))
    return markup

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

# ==================== 依分類瀏覽與批次修改 (新功能) ====================
@bot.message_handler(func=lambda message: message.text == '📂 依分類瀏覽與批次修改')
def browse_start(message):
    markup = InlineKeyboardMarkup()
    markup.row(InlineKeyboardButton("📈 第一維度：治療成效", callback_data="b_dim|1"))
    markup.row(InlineKeyboardButton("📉 第二維度：流失原因", callback_data="b_dim|2"))
    bot.send_message(message.chat.id, "請選擇您要檢視與管理的維度：", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith('b_'))
def handle_browse_callbacks(call):
    chat_id = call.message.chat.id
    message_id = call.message.message_id
    parts = call.data.split('|')
    action = parts[0]

    # 1. 選擇維度，列出該維度所有項目與人數
    if action == 'b_dim':
        dim_type = int(parts[1])
        options = DIM1_OPTIONS if dim_type == 1 else DIM2_OPTIONS
        col_name = 'dim1' if dim_type == 1 else 'dim2'
        dim_title = "第一維度：治療成效" if dim_type == 1 else "第二維度：流失原因"

        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=DictCursor)
        cursor.execute(f"SELECT {col_name} as opt, COUNT(*) as cnt FROM lost_cases GROUP BY {col_name}")
        counts = {row['opt']: row['cnt'] for row in cursor.fetchall()}
        cursor.close()
        conn.close()

        markup = InlineKeyboardMarkup()
        for idx, opt in enumerate(options):
            cnt = counts.get(opt, 0)
            markup.row(InlineKeyboardButton(f"{opt} ({cnt}人)", callback_data=f"b_opt|{dim_type}|{idx}|0"))
        markup.row(InlineKeyboardButton("🔙 返回選擇維度", callback_data="b_home"))

        bot.edit_message_text(f"📂 【{dim_title}】\n請點擊項目查看個案名單：", chat_id, message_id, reply_markup=markup)

    # 返回第一層：維度選擇
    elif action == 'b_home':
        markup = InlineKeyboardMarkup()
        markup.row(InlineKeyboardButton("📈 第一維度：治療成效", callback_data="b_dim|1"))
        markup.row(InlineKeyboardButton("📉 第二維度：流失原因", callback_data="b_dim|2"))
        bot.edit_message_text("請選擇您要檢視與管理的維度：", chat_id, message_id, reply_markup=markup)

    # 2. 進入特定項目，初始化狀態並顯示個案按鈕清單
    elif action == 'b_opt':
        dim_type = int(parts[1])
        opt_idx = int(parts[2])
        page = int(parts[3])
        
        user_batch_state[chat_id] = {
            'dim_type': dim_type,
            'opt_idx': opt_idx,
            'selected_ids': set(),
            'page': page
        }
        render_case_list(chat_id, message_id)

    # 切換分頁
    elif action == 'b_page':
        page = int(parts[1])
        if chat_id in user_batch_state:
            user_batch_state[chat_id]['page'] = page
            render_case_list(chat_id, message_id)

    # 3. 點擊個案按鈕：切換勾選狀態 (☑️ / ⬜)
    elif action == 'b_tog':
        case_id = int(parts[1])
        if chat_id not in user_batch_state:
            bot.answer_callback_query(call.id, "操作已逾時，請重新點擊選單。")
            return
        
        selected = user_batch_state[chat_id]['selected_ids']
        if case_id in selected:
            selected.remove(case_id)
        else:
            selected.add(case_id)
        
        render_case_list(chat_id, message_id)

    # 4. 點擊「批次修改」，列出目標項目供選擇
    elif action == 'b_mod':
        if chat_id not in user_batch_state or not user_batch_state[chat_id]['selected_ids']:
            bot.answer_callback_query(call.id, "⚠️ 請先點擊上方名單勾選至少一位個案！", show_alert=True)
            return
        
        state = user_batch_state[chat_id]
        dim_type = state['dim_type']
        current_opt_idx = state['opt_idx']
        options = DIM1_OPTIONS if dim_type == 1 else DIM2_OPTIONS
        current_opt_name = options[current_opt_idx]
        selected_count = len(state['selected_ids'])

        markup = InlineKeyboardMarkup()
        for idx, opt in enumerate(options):
            if idx != current_opt_idx: # 排除目前的項目
                markup.row(InlineKeyboardButton(f"➡️ 改為：{opt}", callback_data=f"b_save|{idx}"))
        
        markup.row(InlineKeyboardButton("🔙 返回個案列表", callback_data=f"b_page|{state['page']}"))
        
        bot.edit_message_text(
            f"✏️ **批次修改確認**\n\n您目前已從「{current_opt_name}」勾選了 **{selected_count} 位** 個案。\n請選擇要將他們統一改為哪一個項目：",
            chat_id, message_id, reply_markup=markup, parse_mode="Markdown"
        )

    # 5. 執行資料庫批次更新
    elif action == 'b_save':
        new_opt_idx = int(parts[1])
        if chat_id not in user_batch_state or not user_batch_state[chat_id]['selected_ids']:
            bot.answer_callback_query(call.id, "操作已逾時，請重新操作。")
            return

        state = user_batch_state[chat_id]
        dim_type = state['dim_type']
        options = DIM1_OPTIONS if dim_type == 1 else DIM2_OPTIONS
        new_opt_name = options[new_opt_idx]
        selected_ids = list(state['selected_ids'])

        conn = get_db_connection()
        cursor = conn.cursor()
        
        # 同時更新對應維度欄位，以及重新組合 reason 欄位字串，確保資料一致性
        if dim_type == 1:
            cursor.execute('''
                UPDATE lost_cases 
                SET dim1 = %s, reason = %s || ' / ' || dim2 
                WHERE id = ANY(%s)
            ''', (new_opt_name, new_opt_name, selected_ids))
        else:
            cursor.execute('''
                UPDATE lost_cases 
                SET dim2 = %s, reason = dim1 || ' / ' || %s 
                WHERE id = ANY(%s)
            ''', (new_opt_name, new_opt_name, selected_ids))
            
        conn.commit()
        cursor.close()
        conn.close()

        bot.answer_callback_query(call.id, "✅ 批次修改成功！")
        
        # 清空已選狀態，並帶使用者回到上一層(項目列表)查看更新後的人數
        user_batch_state.pop(chat_id, None)
        call.data = f"b_dim|{dim_type}"
        handle_browse_callbacks(call)

def render_case_list(chat_id, message_id):
    state = user_batch_state[chat_id]
    dim_type = state['dim_type']
    opt_idx = state['opt_idx']
    page = state['page']
    selected_ids = state['selected_ids']

    options = DIM1_OPTIONS if dim_type == 1 else DIM2_OPTIONS
    opt_name = options[opt_idx]
    col_name = 'dim1' if dim_type == 1 else 'dim2'

    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=DictCursor)
    cursor.execute(f"SELECT id, case_name, dim1, dim2 FROM lost_cases WHERE {col_name} = %s ORDER BY id DESC", (opt_name,))
    cases = cursor.fetchall()
    cursor.close()
    conn.close()

    total_cases = len(cases)
    page_size = 10 # 每頁顯示 10 人，避免畫面過長
    total_pages = max(1, (total_cases + page_size - 1) // page_size)
    page = max(0, min(page, total_pages - 1))
    state['page'] = page

    start_idx = page * page_size
    end_idx = start_idx + page_size
    page_cases = cases[start_idx:end_idx]

    markup = InlineKeyboardMarkup()
    
    if not page_cases:
        text = f"📂 項目：【{opt_name}】\n\n目前沒有任何個案紀錄。"
    else:
        text = (
            f"📂 項目：【{opt_name}】 (共 {total_cases} 人)\n"
            f"💡 請直接點擊下方個案進行勾選，完成後點擊底部按鈕即可批次修改：\n"
        )
        for c in page_cases:
            cid = c['id']
            cname = c['case_name']
            # 顯示另一個維度的資訊作為輔助參考
            other_dim_info = c['dim2'] if dim_type == 1 else c['dim1']
            prefix = "☑️" if cid in selected_ids else "⬜"
            btn_text = f"{prefix} {cname} ({other_dim_info})"
            markup.row(InlineKeyboardButton(btn_text, callback_data=f"b_tog|{cid}"))

    # 分頁切換按鈕
    if total_pages > 1:
        nav_btns = []
        if page > 0:
            nav_btns.append(InlineKeyboardButton("⬅️ 上一頁", callback_data=f"b_page|{page-1}"))
        nav_btns.append(InlineKeyboardButton(f"第 {page+1}/{total_pages} 頁", callback_data="b_noop"))
        if page < total_pages - 1:
            nav_btns.append(InlineKeyboardButton("➡️ 下一頁", callback_data=f"b_page|{page+1}"))
        markup.row(*nav_btns)

    # 批次修改提交按鈕
    if selected_ids:
        markup.row(InlineKeyboardButton(f"✏️ 批次修改已選個案 ({len(selected_ids)}人)", callback_data="b_mod"))

    markup.row(InlineKeyboardButton("🔙 返回項目列表", callback_data=f"b_dim|{dim_type}"))

    bot.edit_message_text(text, chat_id, message_id, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == 'b_noop')
def handle_noop(call):
    bot.answer_callback_query(call.id)

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
    cursor.execute("SELECT id, case_name, reason, created_at::date FROM lost_cases WHERE case_name LIKE %s OR reason LIKE %s", (f'%{keyword}%', f'%{keyword}%'))
    results = cursor.fetchall()
    cursor.close()
    conn.close()
    
    if results:
        reply = f"🔍 找到關於「{keyword}」的紀錄：\n\n"
        for row in results:
            reply += f"🔹 [ID: {row['id']}] {row['case_name']} \n📝 狀態：{row['reason']} ({row['created_at']})\n\n"
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

# ==================== 單筆修改功能 ====================
@bot.message_handler(func=lambda message: message.text == '✏️ 單筆修改')
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
    
    cursor.execute("SELECT dim1, COUNT(*) as count FROM lost_cases WHERE dim1 != '無' GROUP BY dim1 ORDER BY count DESC")
    dim1_results = cursor.fetchall()
    
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
            
        reply += "\n💡 提示：點擊下方選單的「📂 依分類瀏覽與批次修改」，即可直接點進各分類查看詳細名單並進行批次移動！"
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