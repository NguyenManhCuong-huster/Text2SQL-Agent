"""Builds the deterministic 'shop' sample database (an e-commerce store, 2023-2025)."""
import random
import sqlite3
from datetime import date, datetime, timedelta

from . import config

SCHEMA = """
CREATE TABLE categories (
    category_id   INTEGER PRIMARY KEY,
    name          TEXT NOT NULL,
    description   TEXT
);
CREATE TABLE products (
    product_id    INTEGER PRIMARY KEY,
    name          TEXT NOT NULL,
    category_id   INTEGER NOT NULL REFERENCES categories(category_id),
    unit_price    REAL NOT NULL,      -- selling price in USD
    unit_cost     REAL NOT NULL,      -- purchase cost in USD
    stock_qty     INTEGER NOT NULL,
    is_active     INTEGER NOT NULL DEFAULT 1  -- 1 = currently sold, 0 = discontinued
);
CREATE TABLE customers (
    customer_id   INTEGER PRIMARY KEY,
    full_name     TEXT NOT NULL,
    email         TEXT NOT NULL UNIQUE,
    gender        TEXT,               -- 'M' or 'F'
    birth_date    TEXT,               -- YYYY-MM-DD
    city          TEXT,
    country       TEXT,
    segment       TEXT,               -- 'Consumer', 'Corporate', 'Small Business'
    signup_date   TEXT NOT NULL       -- YYYY-MM-DD
);
CREATE TABLE employees (
    employee_id   INTEGER PRIMARY KEY,
    full_name     TEXT NOT NULL,
    title         TEXT NOT NULL,
    region        TEXT NOT NULL,
    manager_id    INTEGER REFERENCES employees(employee_id),
    hire_date     TEXT NOT NULL,
    salary        REAL NOT NULL
);
CREATE TABLE orders (
    order_id      INTEGER PRIMARY KEY,
    customer_id   INTEGER NOT NULL REFERENCES customers(customer_id),
    employee_id   INTEGER REFERENCES employees(employee_id),  -- sales rep
    order_date    TEXT NOT NULL,      -- YYYY-MM-DD HH:MM:SS
    status        TEXT NOT NULL,      -- 'delivered', 'shipped', 'processing', 'cancelled', 'returned'
    channel       TEXT NOT NULL,      -- 'web', 'mobile', 'store'
    shipping_fee  REAL NOT NULL DEFAULT 0
);
CREATE TABLE order_items (
    order_item_id INTEGER PRIMARY KEY,
    order_id      INTEGER NOT NULL REFERENCES orders(order_id),
    product_id    INTEGER NOT NULL REFERENCES products(product_id),
    quantity      INTEGER NOT NULL,
    unit_price    REAL NOT NULL,      -- price at time of sale
    discount      REAL NOT NULL DEFAULT 0  -- fraction, e.g. 0.1 = 10% off
);
CREATE TABLE payments (
    payment_id    INTEGER PRIMARY KEY,
    order_id      INTEGER NOT NULL REFERENCES orders(order_id),
    method        TEXT NOT NULL,      -- 'card', 'paypal', 'bank_transfer', 'cod'
    amount        REAL NOT NULL,
    paid_at       TEXT                -- NULL if unpaid
);
CREATE TABLE reviews (
    review_id     INTEGER PRIMARY KEY,
    product_id    INTEGER NOT NULL REFERENCES products(product_id),
    customer_id   INTEGER NOT NULL REFERENCES customers(customer_id),
    rating        INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 5),
    comment       TEXT,
    created_at    TEXT NOT NULL
);
CREATE INDEX idx_orders_customer ON orders(customer_id);
CREATE INDEX idx_orders_date ON orders(order_date);
CREATE INDEX idx_items_order ON order_items(order_id);
CREATE INDEX idx_items_product ON order_items(product_id);
"""

CATEGORIES = {
    "Electronics": ("Phones, laptops and gadgets", [
        ("Smartphone X12", 699), ("Smartphone Lite", 299), ("Laptop Pro 14", 1499), ("Laptop Air 13", 999),
        ("Wireless Earbuds", 129), ("Noise-Cancel Headphones", 249), ("Smartwatch S3", 199),
        ("Tablet 11", 449), ("4K Monitor 27", 379), ("Mechanical Keyboard", 89), ("Wireless Mouse", 39),
        ("USB-C Hub", 49), ("Portable SSD 1TB", 119), ("Power Bank 20k", 45),
    ]),
    "Home & Kitchen": ("Appliances and kitchenware", [
        ("Espresso Machine", 329), ("Air Fryer", 119), ("Blender Pro", 89), ("Rice Cooker", 69),
        ("Chef Knife Set", 149), ("Non-stick Pan", 39), ("Robot Vacuum", 279), ("Electric Kettle", 35),
        ("Coffee Grinder", 59), ("Water Filter Pitcher", 29),
    ]),
    "Books": ("Printed books", [
        ("Clean Code", 38), ("Designing Data-Intensive Apps", 45), ("The Pragmatic Programmer", 42),
        ("Deep Learning", 72), ("SQL Cookbook", 49), ("Atomic Habits", 22), ("Sapiens", 24),
        ("The Lean Startup", 20),
    ]),
    "Sports": ("Sports and outdoor equipment", [
        ("Yoga Mat", 29), ("Adjustable Dumbbells", 199), ("Running Shoes", 119), ("Cycling Helmet", 69),
        ("Tennis Racket", 149), ("Camping Tent 2P", 179), ("Water Bottle", 19), ("Fitness Tracker", 79),
    ]),
    "Fashion": ("Clothing and accessories", [
        ("Denim Jacket", 79), ("Leather Belt", 35), ("Cotton T-Shirt", 15), ("Hoodie", 45),
        ("Sneakers Classic", 89), ("Backpack Urban", 65), ("Sunglasses", 55), ("Wool Scarf", 30),
    ]),
    "Beauty": ("Skincare and personal care", [
        ("Face Serum", 39), ("Sunscreen SPF50", 18), ("Moisturizer", 28), ("Electric Toothbrush", 69),
        ("Hair Dryer", 59), ("Perfume 50ml", 95),
    ]),
}

FIRST = ["An", "Binh", "Chi", "Dung", "Giang", "Ha", "Hieu", "Hoa", "Khanh", "Lan", "Linh", "Minh",
         "Nam", "Ngoc", "Phuong", "Quang", "Son", "Thao", "Trang", "Tuan", "Vy", "John", "Emma", "Liam",
         "Olivia", "Noah", "Sophia", "Lucas", "Mia", "Kenji", "Yuki", "Hana", "Min-jun", "Ji-woo",
         "Lukas", "Anna", "Pierre", "Chloe", "Arjun", "Priya"]
LAST = ["Nguyen", "Tran", "Le", "Pham", "Hoang", "Vu", "Dang", "Bui", "Do", "Ngo", "Smith", "Johnson",
        "Brown", "Garcia", "Miller", "Tanaka", "Sato", "Kim", "Park", "Muller", "Schmidt", "Martin",
        "Dubois", "Sharma", "Patel"]
LOCATIONS = [
    ("Hanoi", "Vietnam", 14), ("Ho Chi Minh City", "Vietnam", 18), ("Da Nang", "Vietnam", 6),
    ("New York", "USA", 7), ("San Francisco", "USA", 5), ("Chicago", "USA", 3),
    ("Tokyo", "Japan", 5), ("Osaka", "Japan", 2), ("Seoul", "South Korea", 5),
    ("Berlin", "Germany", 4), ("Munich", "Germany", 2), ("Paris", "France", 4),
    ("Singapore", "Singapore", 6), ("Bangalore", "India", 4), ("Sydney", "Australia", 3),
]
REGIONS = ["APAC", "Americas", "EMEA"]
COUNTRY_REGION = {"Vietnam": "APAC", "Japan": "APAC", "South Korea": "APAC", "Singapore": "APAC",
                  "India": "APAC", "Australia": "APAC", "USA": "Americas", "Germany": "EMEA", "France": "EMEA"}
COMMENTS = {
    5: ["Excellent, highly recommend!", "Perfect, exactly as described.", "Love it!", "Great value."],
    4: ["Very good overall.", "Good quality, fast delivery.", "Works well, minor issues."],
    3: ["Okay for the price.", "Average.", "Does the job."],
    2: ["Not great, expected more.", "Quality could be better."],
    1: ["Terrible, broke after a week.", "Would not buy again.", "Arrived damaged."],
}


def build(path=None) -> None:
    path = path or (config.SOURCES_DIR / "shop.sqlite")
    rng = random.Random(42)
    tmp = path.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    conn = sqlite3.connect(tmp)
    conn.executescript(SCHEMA)

    products = []  # (id, price, cost, popularity)
    pid = 0
    for cid, (cat, (desc, items)) in enumerate(CATEGORIES.items(), start=1):
        conn.execute("INSERT INTO categories VALUES (?,?,?)", (cid, cat, desc))
        for name, price in items:
            pid += 1
            cost = round(price * rng.uniform(0.45, 0.75), 2)
            active = 0 if rng.random() < 0.08 else 1
            conn.execute("INSERT INTO products VALUES (?,?,?,?,?,?,?)",
                         (pid, name, cid, float(price), cost, rng.randint(0, 400), active))
            products.append((pid, float(price), rng.paretovariate(1.6)))

    # Employees: 1 VP, 3 regional managers, 12 reps
    emp = [(1, "Hoang Minh Duc", "VP of Sales", "Global", None, "2018-03-01", 185000.0)]
    eid = 1
    managers = {}
    for region in REGIONS:
        eid += 1
        managers[region] = eid
        emp.append((eid, f"{rng.choice(FIRST)} {rng.choice(LAST)}", "Sales Manager", region, 1,
                    f"{rng.randint(2018, 2020)}-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}",
                    round(rng.uniform(110000, 140000), -2)))
    reps = {r: [] for r in REGIONS}
    for i in range(12):
        region = REGIONS[i % 3]
        eid += 1
        reps[region].append(eid)
        emp.append((eid, f"{rng.choice(FIRST)} {rng.choice(LAST)}", "Sales Representative", region,
                    managers[region], f"{rng.randint(2019, 2024)}-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}",
                    round(rng.uniform(52000, 85000), -2)))
    conn.executemany("INSERT INTO employees VALUES (?,?,?,?,?,?,?)", emp)

    # Customers
    loc_weights = [w for *_, w in LOCATIONS]
    customers = []
    used_emails = set()
    for cid in range(1, 501):
        first, last = rng.choice(FIRST), rng.choice(LAST)
        city, country, _ = rng.choices(LOCATIONS, loc_weights)[0]
        base = f"{first}.{last}".lower().replace("-", "")
        email = f"{base}@example.com"
        n = 1
        while email in used_emails:
            n += 1
            email = f"{base}{n}@example.com"
        used_emails.add(email)
        signup = date(2022, 6, 1) + timedelta(days=rng.randint(0, 1250))
        bdate = date(1960, 1, 1) + timedelta(days=rng.randint(0, 16000))
        segment = rng.choices(["Consumer", "Corporate", "Small Business"], [70, 18, 12])[0]
        conn.execute("INSERT INTO customers VALUES (?,?,?,?,?,?,?,?,?)",
                     (cid, f"{first} {last}", email, rng.choice("MF"), bdate.isoformat(),
                      city, country, segment, signup.isoformat()))
        customers.append((cid, signup, COUNTRY_REGION[country], rng.paretovariate(2.0)))

    # Orders 2023-01-01 .. 2025-12-31 with growth + seasonality
    start, end = datetime(2023, 1, 1), datetime(2025, 12, 31, 23, 59)
    span = (end - start).total_seconds()
    cust_w = [c[3] for c in customers]
    prod_w = [p[2] for p in products]
    oid = item_id = pay_id = 0
    for _ in range(6000):
        # growth: later dates more likely; seasonality: Nov/Dec boost
        while True:
            t = start + timedelta(seconds=rng.random() * span)
            growth = 0.6 + 0.4 * (t - start).total_seconds() / span
            season = 1.6 if t.month in (11, 12) else (0.8 if t.month in (2, 3) else 1.0)
            if rng.random() < growth * season / 1.6:
                break
        cust = rng.choices(customers, cust_w)[0]
        if t.date() < cust[1]:
            t = datetime.combine(cust[1], t.time()) + timedelta(days=rng.randint(0, 30))
            if t > end:
                continue
        oid += 1
        age_days = (end - t).days
        if age_days < 3:
            status = rng.choice(["processing", "shipped"])
        elif age_days < 10:
            status = rng.choices(["shipped", "delivered", "cancelled"], [50, 45, 5])[0]
        else:
            status = rng.choices(["delivered", "cancelled", "returned"], [88, 7, 5])[0]
        channel = rng.choices(["web", "mobile", "store"], [45, 40, 15])[0]
        rep = rng.choice(reps[cust[2]]) if rng.random() < 0.85 else None
        fee = 0.0 if channel == "store" else rng.choice([0.0, 4.99, 9.99])
        conn.execute("INSERT INTO orders VALUES (?,?,?,?,?,?,?)",
                     (oid, cust[0], rep, t.strftime("%Y-%m-%d %H:%M:%S"), status, channel, fee))
        total = fee
        for p in set(rng.choices(products, prod_w, k=rng.choices([1, 2, 3, 4], [50, 30, 14, 6])[0])):
            item_id += 1
            qty = rng.choices([1, 2, 3, 5], [70, 20, 7, 3])[0]
            # prices drift up ~4%/year
            price = round(p[1] * (1 + 0.04 * (t.year - 2023)), 2)
            disc = rng.choices([0, 0.05, 0.1, 0.2], [70, 12, 12, 6])[0]
            if t.month == 11 and t.day >= 24:
                disc = max(disc, 0.25)  # Black Friday week
            conn.execute("INSERT INTO order_items VALUES (?,?,?,?,?,?)",
                         (item_id, oid, p[0], qty, price, disc))
            total += qty * price * (1 - disc)
        if status != "cancelled":
            pay_id += 1
            method = rng.choices(["card", "paypal", "bank_transfer", "cod"], [50, 20, 15, 15])[0]
            paid = None if (status == "processing" and method == "cod") else \
                (t + timedelta(minutes=rng.randint(1, 600))).strftime("%Y-%m-%d %H:%M:%S")
            conn.execute("INSERT INTO payments VALUES (?,?,?,?,?)",
                         (pay_id, oid, method, round(total, 2), paid))

    # Reviews for a subset of delivered order items
    rid = 0
    for oid_, cust_id, odate, prod_id in conn.execute(
        "SELECT o.order_id, o.customer_id, o.order_date, i.product_id FROM orders o "
        "JOIN order_items i ON i.order_id = o.order_id WHERE o.status = 'delivered'"
    ).fetchall():
        if rng.random() < 0.22:
            rid += 1
            quality = (prod_id * 7919) % 10  # some products are consistently better
            rating = max(1, min(5, round(rng.gauss(3.4 + quality * 0.15, 1.0))))
            created = datetime.strptime(odate, "%Y-%m-%d %H:%M:%S") + timedelta(days=rng.randint(3, 30))
            conn.execute("INSERT INTO reviews VALUES (?,?,?,?,?,?)",
                         (rid, prod_id, cust_id, rating, rng.choice(COMMENTS[rating]),
                          created.strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()
    conn.close()
    tmp.replace(path)


SUGGESTIONS = [
    "Top 10 best-selling products by revenue in 2025",
    "Monthly revenue trend from 2023 to 2025",
    "Doanh thu theo từng danh mục sản phẩm năm 2024",
    "Which sales rep generated the most revenue, and who is their manager?",
    "Customers who placed at least 8 orders but never left a review",
    "Average rating per category, only products with at least 10 reviews",
    "Share of orders by channel and payment method",
    "Gross margin by category for delivered orders",
]


def ensure_sample() -> None:
    if not (config.SOURCES_DIR / "shop.sqlite").exists():
        build()
