import requests
from bs4 import BeautifulSoup
import time
from typing import List
import random
import asyncpg
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from playwright.sync_api import sync_playwright
import uvicorn
import asyncio
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Optional
from datetime import datetime
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests 
from fpdf import FPDF
from fastapi.responses import FileResponse
import os
from pathlib import Path
from gigachat import GigaChat
import sys
import bcrypt

BASE_DIR = Path(__file__).resolve().parent

# Render PostgreSQL URL
DATABASE_URL = "postgresql://neondb_owner:npg_Z6wpVujMoK5f@ep-damp-breeze-alfac21w-pooler.c-3.eu-central-1.aws.neon.tech/neondb?sslmode=require&channel_binding=require"
GOOGLE_CLIENT_ID = "366926202736-8epgi6ls1uul9o0662902pp57fg1i4oo.apps.googleusercontent.com"

def hash_password(password: str) -> str:
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(password.encode('utf-8'), salt).decode('utf-8')

def verify_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode('utf-8'), hashed.encode('utf-8'))

from contextlib import asynccontextmanager
executor = ThreadPoolExecutor(max_workers=2)
@asynccontextmanager
async def lifespan(app: FastAPI):
    global db_pool
    db_pool = None
    try:
        print("🔄 Подключение к Render PostgreSQL...")
        
        db_pool = await asyncpg.create_pool(
            DATABASE_URL,
            min_size=1,
            max_size=10,
            statement_cache_size=0,
            command_timeout=90.0,
            timeout=60.0,
            ssl='require',
            server_settings={'application_name': 'ticketsearch-app'}
        )
        
        # Тест соединения
        async with db_pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
            
        print("✅ Database pool created and tested successfully!")
        
    except Exception as e:
        print(f"❌ Ошибка создания пула БД: {e}")
        raise
    yield
    if db_pool:
        await db_pool.close()
        print("🔌 Database pool closed")

app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

db_pool = None

giga_client = GigaChat(
    credentials="MDE5Y2Y2ODMtNjg1Ni03NDQ5LWE1NjEtYjA1M2UyOGNmZmM1Ojc2ODQwMWMzLTQyZWEtNDM4MS05NGQ3LTgxMGNhYTE4ZjdhZg==", 
    verify_ssl_certs=False
)

# ==================== Pydantic модели ====================

class GoogleToken(BaseModel):
    token: str

class UserUpdate(BaseModel):
    name: str
    last_name: str

class PasswordChange(BaseModel):
    current_password: str
    new_password: str

class Deepsek(BaseModel):
    message: str

class UserRegister(BaseModel):
    name: str
    last_name: str
    email: str
    password: str

class UserLogin(BaseModel):
    email: str
    password: str

class BookingData(BaseModel):
    user_id: Optional[int] = None 
    destination: str
    route: str
    departure_date: str
    train_number: str
    carriage_type: str
    price: float
    email: Optional[str] = None
    passenger_fio: Optional[str] = None
    passport_serias: Optional[str] = None
    passport_number: Optional[str] = None

class SearchQuery(BaseModel):
    departure: str
    arrival: str
    date: str

class WagonRequest(BaseModel):
    from_city: str
    to_city: str
    date: str
    type: str
    target_time: Optional[str] = None 

# ==================== Функции парсинга ====================

def get_ufs_data(departure, arrival, date, wagon_type, target_time: Optional[str] = None):
    """Синхронная версия — стабильнее на Windows"""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
            viewport={'width': 1920, 'height': 1080}
        )
        page = context.new_page()
        
        url = f"https://www.ufs-online.ru/kupit-zhd-bilety/{departure}/{arrival}?date={date}"
        print(url)
        
        try:
            page.goto(url, wait_until="load", timeout=60000)
            time.sleep(5)

            page.wait_for_selector(".wg-train-container", timeout=20000)
            train_cards = page.query_selector_all(".wg-train-container")
            
            selected_card = None
            if target_time:
                for card in train_cards:
                    time_elem = card.query_selector(".wg-track-info__time")
                    if time_elem and target_time in time_elem.inner_text():
                        selected_card = card
                        break
                if not selected_card:
                    return None
            else:
                selected_card = train_cards[0] if train_cards else None
            
            if not selected_card:
                return None
            
            wagon_buttons = selected_card.query_selector_all(".wg-wagon-type__item")
            
            target_btn = None
            for btn in wagon_buttons:
                if wagon_type.lower() in btn.inner_text().lower():
                    target_btn = btn
                    break
            
            if not target_btn:
                return None

            target_btn.scroll_into_view_if_needed()
            target_btn.click()
            time.sleep(3)

            page.wait_for_selector(".wg-seats-button", timeout=20000)
            wagon_buttons = page.query_selector_all(".wg-seats-button")
            
            results = []
            for btn in wagon_buttons:
                btn_info = btn.inner_text()
                w_num = "".join(filter(str.isdigit, btn_info.split('\n')[0]))
                
                price_elem = btn.query_selector(".wg-seats-button_price")
                price = "".join(filter(str.isdigit, price_elem.inner_text())) if price_elem else "0"
                
                btn.click()
                time.sleep(2)
                
                free_seats = parse_wagon_seats_sync(page)
                
                results.append({
                    "wagon_number": w_num,
                    "price": f"{price} руб.",
                    "seats": free_seats,
                    "count": len(free_seats)
                })

            return results
        
        except Exception as e:
            print(f"Playwright error: {e}")
            return None
        finally:
            browser.close()

def parse_wagon_seats_sync(page):
    try:
        page.wait_for_selector(".wg-car__box", timeout=20000)
        time.sleep(2.5)
        
        seats_elements = page.query_selector_all(".sa-scheme__berth-place:not(.sa-scheme__berth-place_disabled)")
        
        seats = []
        for el in seats_elements:
            text = el.inner_text()
            val = text.strip()
            if val.isdigit():
                seats.append(val)
        
        return sorted(list(set(seats)), key=int)
    except Exception:
        return []

def clean_price(price_str):
    if not price_str: return None
    price_str = price_str.replace('\xa0', '').replace(' ', '').replace(',', '.')
    cleaned = ""
    dot_found = False
    for char in price_str:
        if char.isdigit(): cleaned += char
        elif char == '.' and not dot_found:
            cleaned += char
            dot_found = True
    try:
        return int(float(cleaned)) if cleaned else None
    except:
        return None

def get_tickets_from_web(city1, city2, date):
    url = f'https://www.ufs-online.ru/kupit-zhd-bilety/{city1}/{city2}?date={date}'
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}
    
    try:
        time.sleep(random.uniform(0.6, 1.3))
        response = requests.get(url, headers=headers, timeout=30)
        soup = BeautifulSoup(response.text, 'lxml')
        results = []
        tickets = soup.find_all('div', class_='wg-train-container')

        for idx, ticket in enumerate(tickets):
            train_tag = ticket.find('a', class_='wg-train-info__number-link')
            train_num = train_tag.get_text(strip=True) if train_tag else "Н/Д"

            time_tags = ticket.find_all('span', class_='wg-track-info__time')
            dep_time = time_tags[0].get_text(strip=True) if len(time_tags) > 0 else "--:--"
            arr_time = time_tags[1].get_text(strip=True) if len(time_tags) > 1 else "--:--"

            dirs = ticket.find_all('span', class_='wg-track-info__direction')
            stats = ticket.find_all('span', class_='wg-track-info__station')
            dep_city = dirs[0].get_text(strip=True) if len(dirs) > 0 else ""
            arr_city = dirs[1].get_text(strip=True) if len(dirs) > 1 else ""
            dep_station = stats[0].get_text(strip=True) if len(stats) > 0 else ""
            arr_station = stats[1].get_text(strip=True) if len(stats) > 1 else ""

            date_tags = ticket.find_all('span', class_='wg-track-info__date')
            dep_date = date_tags[0].get_text(strip=True) if len(date_tags) > 0 else ""
            arr_date = date_tags[1].get_text(strip=True) if len(date_tags) > 1 else dep_date

            route_block = ticket.find('div', class_='wg-train-info__direction')
            if route_block:
                full_route = route_block.get_text(" ", strip=True)
                full_route = full_route.replace(" → ", " → ").replace("  ", " ")
            else:
                full_route = f"{city1.replace('-', ' ').capitalize()} → {city2.replace('-', ' ').capitalize()}"

            prices_dict = {k: "—" for k in ["Базовый", "Эконом", "Эконом+", "Семейный", "Бистро", "Бизнес", "Первый", "Купе-Сьют", "Сидячий", "Плацкарт", "Купе", "СВ", "Люкс"]}
            min_p = 0
            
            for item in ticket.find_all('div', class_='wg-wagon-type__item'):
                t_tag = item.find('div', class_='wg-wagon-type__title')
                p_tag = item.find('span', class_='wg-wagon-type__price-value')
                s_tag = item.find('span', class_='wg-wagon-type__available-seats')
                
                if t_tag and p_tag:
                    title = t_tag.get_text(strip=True).lower()
                    p_int = clean_price(p_tag.get_text(strip=True))
                    s_count = "".join(filter(str.isdigit, s_tag.get_text(strip=True))) if s_tag else "0"
                    
                    if p_int:
                        info = {"price": str(p_int), "seats": s_count}
                        if "базовый" in title: prices_dict["Базовый"] = info
                        elif "эконом +" in title or "эконом+" in title: prices_dict["Эконом+"] = info
                        elif "эконом" in title: prices_dict["Эконом"] = info
                        elif "семейный" in title: prices_dict["Семейный"] = info
                        elif "бистро" in title: prices_dict["Бистро"] = info
                        elif "бизнес" in title: prices_dict["Бизнес"] = info
                        elif "первый" in title: prices_dict["Первый"] = info
                        elif "сьют" in title: prices_dict["Купе-Сьют"] = info
                        elif "сидяч" in title: prices_dict["Сидячий"] = info
                        elif "плацкарт" in title: prices_dict["Плацкарт"] = info
                        elif "купе" in title: prices_dict["Купе"] = info
                        elif "св" in title: prices_dict["СВ"] = info
                        elif "люкс" in title: prices_dict["Люкс"] = info

                        if min_p == 0 or p_int < min_p: min_p = p_int

            results.append({
                "id": idx, 
                "train": train_num, 
                "departure_time": dep_time,
                "departure_date": dep_date, 
                "arrival_time": arr_time,
                "arrival_date": arr_date,
                "dep_city": dep_city, 
                "arr_city": arr_city, 
                "dep_station": dep_station, 
                "arr_station": arr_station,
                "price": min_p if min_p > 0 else "Н/Д", 
                "prices_all": prices_dict, 
                "route": full_route 
            })
        return results
    except Exception as e:
        print(f"Error: {e}")
        return []

def send_email(data: BookingData):
    sender_email = "ticketsearch406@gmail.com"
    password = "zlfa zska fhdh vqcy" 

    msg = MIMEMultipart("alternative")
    msg['From'] = f"Ticket Search <{sender_email}>"
    msg['To'] = data.email
    msg['Subject'] = f"Ваш электронный билет на поезд {data.train_number}"

    html_body = f"""
    <html>
    <body style="margin: 0; padding: 0; background-color: #f6f9fc; font-family: 'Segoe UI', Arial, sans-serif;">
        <table width="100%" border="0" cellspacing="0" cellpadding="0">
            <tr>
                <td align="center" style="padding: 20px;">
                    <table width="600" border="0" cellspacing="0" cellpadding="0" style="background-color: #ffffff; border-radius: 12px; overflow: hidden; box-shadow: 0 4px 15px rgba(0,0,0,0.1);">
                        <tr>
                            <td style="background-color: #1a237e; padding: 30px; text-align: center;">
                                <h1 style="color: #ffffff; margin: 0; font-size: 26px; letter-spacing: 1px;">ЭЛЕКТРОННЫЙ БИЛЕТ</h1>
                                <p style="color: #bbdefb; margin: 8px 0 0; font-size: 16px;">Удачной поездки, {data.passenger_fio}!</p>
                            </td>
                        </tr>
                        <tr>
                            <td style="padding: 30px;">
                                <table width="100%" border="0" cellspacing="0" cellpadding="0">
                                    <tr>
                                        <td style="padding-bottom: 25px; border-bottom: 2px dashed #e0e0e0;">
                                            <div style="font-size: 12px; color: #7f8c8d; text-transform: uppercase; font-weight: bold;">Маршрут</div>
                                            <div style="font-size: 18px; font-weight: bold; color: #2c3e50; margin-top: 5px;">{data.route}</div>
                                        </td>
                                    </tr>
                                    <tr>
                                        <td style="padding: 25px 0;">
                                            <table width="100%" border="0" cellspacing="0" cellpadding="0">
                                                <tr>
                                                    <td width="50%" style="vertical-align: top;">
                                                        <div style="font-size: 12px; color: #7f8c8d; text-transform: uppercase;">Поезд</div>
                                                        <div style="font-size: 16px; font-weight: bold; color: #2c3e50;">№ {data.train_number}</div>
                                                    </td>
                                                    <td width="50%" style="vertical-align: top;">
                                                        <div style="font-size: 12px; color: #7f8c8d; text-transform: uppercase;">Вагон</div>
                                                        <div style="font-size: 16px; font-weight: bold; color: #2c3e50;">{data.carriage_type}</div>
                                                    </td>
                                                </tr>
                                            </table>
                                        </td>
                                    </tr>
                                    <tr>
                                        <td style="padding: 20px; background-color: #f8f9fa; border-radius: 8px; border-left: 4px solid #1a237e;">
                                            <div style="font-size: 12px; color: #7f8c8d; margin-bottom: 5px;">ДАННЫЕ ПАССАЖИРА</div>
                                            <div style="font-size: 16px; font-weight: bold; color: #2c3e50;">{data.passenger_fio}</div>
                                            <div style="font-size: 14px; color: #34495e;">Документ: {data.passport_serias}    {data.passport_number}</div>
                                        </td>
                                    </tr>
                                </table>
                            </td>
                        </tr>
                        <tr>
                            <td style="background-color: #f1f3f8; padding: 25px; text-align: center; border-top: 1px solid #e0e0e0;">
                                <div style="font-size: 14px; color: #7f8c8d;">ИТОГО К ОПЛАТЕ</div>
                                <div style="font-size: 32px; font-weight: bold; color: #1a237e; margin-top: 5px;">{data.price} ₽</div>
                            </td>
                        </tr>
                    </table>
                </td>
            </tr>
        </table>
    </body>
    </html>
    """
    msg.attach(MIMEText(html_body, 'html'))

    try:
        server = smtplib.SMTP_SSL('smtp.gmail.com', 465)
        server.login(sender_email, password)
        server.send_message(msg)
        server.quit()
        return True
    except Exception as e:
        print(f"Ошибка почты: {e}")
        return False

# ==================== Эндпоинты API ====================

@app.post("/get-wagons")
async def api_get_wagons(request: WagonRequest):
    try:
        # Запускаем синхронную функцию в отдельном потоке, чтобы не блокировать основной цикл
        loop = asyncio.get_event_loop()
        data = await loop.run_in_executor(
            executor,
            get_ufs_data,
            request.from_city,
            request.to_city,
            request.date,
            request.type,
            request.target_time
        )
        
        if data is None:
            raise HTTPException(status_code=404, detail=f"Категория {request.type} не найдена на этом поезде")
            
        return {"status": "success", "data": data}
    except Exception as e:
        # Логируем полную ошибку для отладки
        print(f"Критическая ошибка в /get-wagons: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        # Возвращаем понятное сообщение
        raise HTTPException(status_code=500, detail=f"Ошибка парсинга: {str(e)}")

@app.post("/register")
async def register(user: UserRegister):
    if not db_pool:
        raise HTTPException(status_code=503, detail="База данных недоступна")
    
    async with db_pool.acquire() as conn:
        try:
            existing = await conn.fetchrow("SELECT id FROM users_ticket WHERE email = $1", user.email)
            if existing: 
                raise HTTPException(status_code=400, detail="Пользователь уже существует")
            
            hashed_password = hash_password(user.password)
            
            row = await conn.fetchrow(
                "INSERT INTO users_ticket (name, last_name, email, password, role) "
                "VALUES ($1, $2, $3, $4, $5) RETURNING id, name, last_name, email, role",
                user.name, user.last_name, user.email, hashed_password, 'user'
            )
            return {"status": "success", "user": dict(row)}
        except Exception as e:
            print(f"Register error: {e}")
            raise HTTPException(status_code=500, detail="Ошибка регистрации")

@app.post("/login")
async def login(user: UserLogin):
    if not db_pool:
        raise HTTPException(status_code=503, detail="База данных недоступна")
    
    async with db_pool.acquire() as conn:
        try:
            row = await conn.fetchrow(
                "SELECT id, name, last_name, email, password, role FROM users_ticket WHERE email = $1", 
                user.email
            )
            
            if not row:
                raise HTTPException(status_code=401, detail="Неверный email или пароль")
            
            if not verify_password(user.password, row["password"]):
                raise HTTPException(status_code=401, detail="Неверный email или пароль")
            
            return {
                "status": "success", 
                "user": {
                    "id": row["id"],
                    "name": row["name"],
                    "last_name": row["last_name"],
                    "email": row["email"],
                    "role": row["role"]
                }
            }
        except Exception as e:
            print(f"Login error: {e}")
            raise HTTPException(status_code=500, detail="Ошибка сервера")

@app.post("/search")
async def search_tickets(query: SearchQuery):
    return get_tickets_from_web(query.departure.lower(), query.arrival.lower(), query.date)

@app.get("/my-tickets/{user_id}")
async def get_user_tickets(user_id: int):
    if not db_pool:
        raise HTTPException(status_code=503, detail="База данных недоступна")
    
    async with db_pool.acquire() as conn:
        try:
            rows = await conn.fetch(
                "SELECT id, destination, departure_date, train_number, carriage_type, price "
                "FROM tickets WHERE user_id = $1 ORDER BY departure_date DESC",
                user_id
            )
            tickets = []
            for row in rows:
                ticket = dict(row)
                ticket['departure_date'] = ticket['departure_date'].strftime('%Y-%m-%d')
                ticket['price'] = float(ticket['price'])
                tickets.append(ticket)
            return tickets
        except Exception as e:
            print(f"Ошибка получения билетов: {e}")
            raise HTTPException(status_code=500, detail="Ошибка сервера")

@app.get("/download-ticket/{ticket_id}")
async def download_ticket(ticket_id: int):
    if not db_pool:
        raise HTTPException(status_code=503, detail="База данных недоступна")
    
    async with db_pool.acquire() as conn:
        try:
            ticket = await conn.fetchrow("""
                SELECT t.*, u.name, u.last_name, u.email 
                FROM tickets t 
                JOIN users_ticket u ON t.user_id = u.id 
                WHERE t.id = $1
            """, ticket_id)

            if not ticket:
                raise HTTPException(status_code=404, detail="Билет не найден")

            pdf = FPDF(orientation='P', unit='mm', format='A4')
            pdf.add_page()
            
            dark_blue = (26, 35, 126)
            light_blue = (232, 240, 254)
            gold = (255, 215, 0)
            gray = (128, 128, 128)
            light_gray = (245, 245, 245)

            font_path = BASE_DIR / "DejaVuSans.ttf"
            
            if font_path.exists():
                pdf.add_font("DejaVu", "", str(font_path), uni=True)
                pdf.add_font("DejaVu", "B", str(BASE_DIR / "DejaVuSans-Bold.ttf"), uni=True)
                pdf.set_font("DejaVu", size=10)
            else:
                pdf.set_font("Arial", size=10)

            pdf.set_fill_color(*dark_blue)
            pdf.rect(0, 0, 210, 40, 'F')
            
            pdf.set_text_color(255, 255, 255)
            pdf.set_font("DejaVu", "B", size=20)
            pdf.set_xy(20, 12)
            pdf.cell(0, 10, "TICKET SEARCH", ln=False)
            
            pdf.set_font("DejaVu", size=10)
            pdf.set_xy(20, 25)
            pdf.cell(0, 10, "Электронный билет", ln=False)

            pdf.set_text_color(200, 200, 200)
            pdf.set_xy(150, 15)
            pdf.set_font("DejaVu", size=8)
            pdf.cell(0, 5, f"Билет № {ticket_id:06d}", ln=False)
            pdf.set_xy(150, 22)
            current_date = datetime.now().strftime('%d.%m.%Y')
            pdf.cell(0, 5, f"Дата: {current_date}", ln=False)

            pdf.set_text_color(0, 0, 0)
            pdf.set_y(50)

            pdf.set_font("DejaVu", "B", size=16)
            pdf.set_text_color(*dark_blue)
            pdf.cell(0, 10, ticket['destination'], ln=True, align='C')
            
            pdf.set_font("DejaVu", size=10)
            pdf.set_text_color(*gray)
            pdf.cell(0, 10, "Маршрут следования", ln=True, align='C')
            pdf.ln(10)

            pdf.set_fill_color(*light_gray)
            pdf.rect(20, 80, 170, 60, 'F')
            
            route_parts = ticket['destination'].split(' — ')
            if len(route_parts) == 2:
                departure, arrival = route_parts
            else:
                departure = ticket['destination']
                arrival = ""

            pdf.set_xy(30, 85)
            pdf.set_font("DejaVu", size=8)
            pdf.set_text_color(*gray)
            pdf.cell(0, 5, "ОТПРАВЛЕНИЕ", ln=False)
            
            pdf.set_xy(30, 92)
            pdf.set_font("DejaVu", "B", size=14)
            pdf.set_text_color(*dark_blue)
            pdf.cell(0, 8, departure, ln=False)

            pdf.set_xy(100, 92)
            pdf.set_font("DejaVu", size=20)
            pdf.set_text_color(*gold)
            pdf.cell(0, 8, "→", ln=False)

            pdf.set_xy(130, 85)
            pdf.set_font("DejaVu", size=8)
            pdf.set_text_color(*gray)
            pdf.cell(0, 5, "ПРИБЫТИЕ", ln=False)
            
            pdf.set_xy(130, 92)
            pdf.set_font("DejaVu", "B", size=14)
            pdf.set_text_color(*dark_blue)
            pdf.cell(0, 8, arrival, ln=False)

            dep_date_obj = ticket['departure_date']
            if isinstance(dep_date_obj, str):
                dep_date = datetime.strptime(dep_date_obj, '%Y-%m-%d')
            else:
                dep_date = dep_date_obj
                
            pdf.set_xy(30, 115)
            pdf.set_font("DejaVu", size=8)
            pdf.set_text_color(*gray)
            pdf.cell(0, 5, "ДАТА ПОЕЗДКИ", ln=False)
            
            pdf.set_xy(30, 122)
            pdf.set_font("DejaVu", "B", size=12)
            pdf.set_text_color(*dark_blue)
            pdf.cell(0, 6, dep_date.strftime('%d %B %Y'), ln=False)

            pdf.set_xy(130, 115)
            pdf.set_font("DejaVu", size=8)
            pdf.set_text_color(*gray)
            pdf.cell(0, 5, "ПОЕЗД", ln=False)
            
            pdf.set_xy(130, 122)
            pdf.set_font("DejaVu", "B", size=12)
            pdf.set_text_color(*dark_blue)
            pdf.cell(0, 6, f"№ {ticket['train_number']}", ln=False)

            pdf.set_y(150)
            pdf.set_fill_color(*light_blue)
            pdf.rect(20, 150, 170, 40, 'F')
            
            pdf.set_xy(30, 155)
            pdf.set_font("DejaVu", "B", size=12)
            pdf.set_text_color(*dark_blue)
            pdf.cell(0, 8, "ИНФОРМАЦИЯ О ПАССАЖИРЕ", ln=False)
            
            pdf.set_xy(30, 168)
            pdf.set_font("DejaVu", size=10)
            pdf.set_text_color(0, 0, 0)
            passenger_name = f"{ticket['name']} {ticket['last_name']}"
            pdf.cell(0, 6, f"ФИО: {passenger_name}", ln=False)

            pdf.set_y(200)
            
            pdf.set_xy(30, 200)
            pdf.set_font("DejaVu", size=8)
            pdf.set_text_color(*gray)
            pdf.cell(0, 5, "ТИП ВАГОНА", ln=False)
            
            pdf.set_xy(30, 208)
            pdf.set_font("DejaVu", "B", size=11)
            pdf.set_text_color(*dark_blue)
            pdf.cell(0, 6, ticket['carriage_type'], ln=False)

            pdf.set_xy(130, 200)
            pdf.set_font("DejaVu", size=8)
            pdf.set_text_color(*gray)
            pdf.cell(0, 5, "ЦЕНА", ln=False)
            
            pdf.set_xy(130, 208)
            pdf.set_font("DejaVu", "B", size=14)
            pdf.set_text_color(*dark_blue)
            pdf.cell(0, 6, f"{ticket['price']} ₽", ln=False)

            pdf.set_y(230)
            pdf.set_font("DejaVu", size=6)
            pdf.set_text_color(*gray)
            pdf.cell(0, 5, "Штрих-код:", ln=True, align='C')
            
            pdf.set_draw_color(*dark_blue)
            pdf.set_line_width(0.5)
            x = 55
            for i in range(20):
                height = 10 + (i % 3) * 5
                pdf.line(x + i*5, 240, x + i*5, 240 + height)
            
            pdf.set_y(260)
            pdf.set_font("DejaVu", size=7)
            pdf.set_text_color(*gray)
            pdf.cell(0, 5, "Данный билет является электронным и действителен при предъявлении", ln=True, align='C')
            pdf.cell(0, 5, "документа, удостоверяющего личность", ln=True, align='C')
            
            pdf.set_y(275)
            pdf.set_dash_pattern(2, 2)
            pdf.set_line_width(0.2)
            pdf.line(20, 275, 190, 275)
            
            pdf.set_y(280)
            pdf.set_font("DejaVu", size=6)
            pdf.set_text_color(*gray)
            pdf.cell(0, 3, "При посадке предъявите этот билет (в электронном или бумажном виде) и паспорт.", ln=True, align='C')

            filename = f"ticket_{ticket_id}.pdf"
            file_path = BASE_DIR / filename

            pdf.output(str(file_path))

            return FileResponse(
                path=str(file_path),
                media_type="application/pdf",
                filename=f"билет_{ticket['train_number']}_{dep_date.strftime('%d%m%Y')}.pdf",
                headers={
                    "Content-Disposition": f'attachment; filename="ticket_{ticket_id}.pdf"'
                }
            )

        except Exception as e:
            print(f"Ошибка PDF: {e}")
            raise HTTPException(status_code=500, detail=str(e))

@app.post("/send-ticket")
async def send_ticket_endpoint(data: BookingData):
    if not data.user_id:
        return {"status": "success"}

    async with db_pool.acquire() as conn:
        try:
            valid_date = datetime.strptime(str(data.departure_date), "%Y-%m-%d").date()
            await conn.execute(
                """INSERT INTO tickets 
                   (user_id, destination, departure_date, train_number, carriage_type, price)
                   VALUES ($1, $2, $3, $4, $5, $6)""",
                data.user_id, data.route, valid_date, 
                data.train_number, data.carriage_type, data.price
            )
            return {"status": "success"}
        except Exception as e:
            print(f"Ошибка сохранения билета: {e}")
            raise HTTPException(status_code=500, detail="Ошибка сохранения билета в базу")

@app.post("/auth/google")
async def auth_google(data: GoogleToken):
    try:
        idinfo = id_token.verify_oauth2_token(
            data.token, 
            google_requests.Request(), 
            GOOGLE_CLIENT_ID
        )
        email = idinfo['email']
        name = idinfo.get('given_name', 'User')
        last_name = idinfo.get('family_name', '')

        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, name, last_name, email FROM users_ticket WHERE email = $1", 
                email
            )
            
            if not row:
                row = await conn.fetchrow(
                    """INSERT INTO users_ticket (name, last_name, email, password, role) 
                       VALUES ($1, $2, $3, $4, $5) 
                       RETURNING id, name, last_name, email, role""",
                    name, last_name, email, "google_auth_user", "user"
                )
            
            return {"status": "success", "user": dict(row)}
            
    except ValueError:
        raise HTTPException(status_code=400, detail="Неверный Google токен")
    except Exception as e:
        print(f"Google auth error: {e}")
        raise HTTPException(status_code=500, detail="Ошибка авторизации")

@app.post("/gigaChat")
async def set_giga_ai(data: Deepsek):
    try:
        response = giga_client.chat({
            "messages": [
                {
                    "role": "system",
                    "content": "Ты ассистент TicketSearch сайта по по продаже ЖД билетов(учебного проекта). Отвечай кратко (1-3 предложения)."
                },
                {
                    "role": "user",
                    "content": data.message
                }
            ],
            "model": "GigaChat:latest",
            "temperature": 0.7
        })

        reply_text = response.choices[0].message.content
        return {"reply": reply_text.strip()}

    except Exception as e:
        print(f"Ошибка GigaChat: {e}")
        raise HTTPException(status_code=500, detail="Сервис GigaChat временно недоступен")

@app.get("/users/{user_id}")
async def get_user_profile(user_id: int):
    if not db_pool:
        raise HTTPException(status_code=503, detail="База данных недоступна")
    
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, name, last_name, email FROM users_ticket WHERE id = $1", 
            user_id
        )
        if not row:
            raise HTTPException(status_code=404, detail="Пользователь не найден")
        return dict(row)

@app.put("/users/{user_id}")
async def update_user_profile(user_id: int, data: UserUpdate):
    if not db_pool:
        raise HTTPException(status_code=503, detail="База данных недоступна")
    
    async with db_pool.acquire() as conn:
        exists = await conn.fetchrow("SELECT id FROM users_ticket WHERE id = $1", user_id)
        if not exists:
            raise HTTPException(status_code=404, detail="Пользователь не найден")
        
        row = await conn.fetchrow(
            """UPDATE users_ticket 
               SET name = $1, last_name = $2 
               WHERE id = $3 
               RETURNING id, name, last_name, email""",
            data.name, data.last_name, user_id
        )
        return dict(row)

@app.post("/users/{user_id}/change-password")
async def change_password(user_id: int, data: PasswordChange):
    if not db_pool:
        raise HTTPException(status_code=503, detail="База данных недоступна")
    
    async with db_pool.acquire() as conn:
        user = await conn.fetchrow(
            "SELECT password FROM users_ticket WHERE id = $1", user_id
        )
        if not user:
            raise HTTPException(status_code=404, detail="Пользователь не найден")
        
        if not verify_password(data.current_password, user['password']):
            raise HTTPException(status_code=400, detail="Неверный текущий пароль")
        
        await conn.execute(
            "UPDATE users_ticket SET password = $1 WHERE id = $2",
            hash_password(data.new_password), user_id
        )
        
        return {"status": "success", "message": "Пароль успешно изменён"}

@app.delete("/users/{user_id}")
async def delete_account(user_id: int):
    if not db_pool:
        raise HTTPException(status_code=503, detail="База данных недоступна")
    
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM tickets WHERE user_id = $1", user_id)
        await conn.execute("DELETE FROM users_ticket WHERE id = $1", user_id)
        return {"status": "success", "message": "Аккаунт удалён"}

@app.get("/news")
async def get_news():
    if not db_pool:
        raise HTTPException(status_code=503, detail="База данных недоступна")
    
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM news ORDER BY news_date DESC"
        )
        news_list = []
        for row in rows:
            item = dict(row)
            if item.get('news_date'):
                item['news_date'] = item['news_date'].strftime('%d.%m.%Y')
            news_list.append(item)
        return news_list

@app.get("/api/dashboard-stats")
async def get_dashboard_stats():
    if not db_pool:
        raise HTTPException(status_code=503, detail="База данных недоступна")

    async with db_pool.acquire() as conn:
        async with conn.transaction():
            stats = await conn.fetchrow("""
                SELECT 
                    COALESCE(SUM(price), 0) as revenue,
                    COUNT(*) as tickets_sold,
                    (SELECT COUNT(*) FROM users_ticket) as users_count
                FROM tickets
            """)

            transactions = await conn.fetch("""
                SELECT 
                    t.id, 
                    u.name || ' ' || COALESCE(u.last_name, '') as passenger_name, 
                    t.destination as route, 
                    t.price,
                    t.created_at
                FROM tickets t
                JOIN users_ticket u ON t.user_id = u.id
                ORDER BY t.created_at DESC 
                LIMIT 5
            """)

            return {
                "revenue": float(stats["revenue"]),
                "tickets_sold": stats["tickets_sold"],
                "users_count": stats["users_count"],
                "transactions": [
                    {
                        "id": f"RZD-{t['id']}", 
                        "passenger_name": t['passenger_name'], 
                        "route": t['route'], 
                        "price": float(t['price']),
                        "date": t['created_at'].strftime("%d.%m.%Y")
                    } for t in transactions
                ]
            }

if __name__ == "__main__":
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    
    config = uvicorn.Config(app, host="127.0.0.1", port=8000, log_level="info")
    server = uvicorn.Server(config)
    asyncio.run(server.serve())
