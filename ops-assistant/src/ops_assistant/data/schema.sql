PRAGMA foreign_keys = ON;

CREATE TABLE regions (
    region_id   TEXT PRIMARY KEY,
    region_name TEXT NOT NULL UNIQUE,
    manager     TEXT NOT NULL
);

CREATE TABLE products (
    product_id       TEXT PRIMARY KEY,
    product_name     TEXT NOT NULL,
    category         TEXT NOT NULL,
    unit_price_cents INTEGER NOT NULL CHECK (unit_price_cents > 0)
);

CREATE TABLE orders (
    order_id         TEXT PRIMARY KEY,
    order_date       TEXT NOT NULL,              -- YYYY-MM-DD
    region_id        TEXT NOT NULL REFERENCES regions(region_id),
    product_id       TEXT NOT NULL REFERENCES products(product_id),
    quantity         INTEGER NOT NULL CHECK (quantity > 0),
    unit_price_cents INTEGER NOT NULL CHECK (unit_price_cents > 0),
    channel          TEXT NOT NULL               -- 线下/天猫/京东/抖音；仅存不分析
);

CREATE TABLE refunds (
    refund_id   TEXT PRIMARY KEY,
    order_id    TEXT NOT NULL REFERENCES orders(order_id),
    refund_date TEXT NOT NULL,                   -- YYYY-MM-DD
    amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
    reason      TEXT NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ('approved','pending','rejected'))
);

CREATE INDEX idx_orders_date ON orders(order_date);
CREATE INDEX idx_orders_region ON orders(region_id);
CREATE INDEX idx_refunds_date ON refunds(refund_date);
CREATE INDEX idx_refunds_status ON refunds(status);
