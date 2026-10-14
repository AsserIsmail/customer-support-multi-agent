CREATE TABLE customers (
    id NUMBER(19,0) PRIMARY KEY,
    name VARCHAR2(1000 CHAR) NOT NULL,
    email VARCHAR2(1000 CHAR) NOT NULL UNIQUE,
    joined_on DATE NOT NULL,
    plan VARCHAR2(20 CHAR) NOT NULL CHECK (plan IN ('basic', 'pro', 'enterprise'))
);
CREATE TABLE orders (
    id NUMBER(19,0) PRIMARY KEY,
    customer_id NUMBER(19,0) NOT NULL REFERENCES customers(id),
    placed_on DATE NOT NULL,
    product VARCHAR2(1000 CHAR) NOT NULL,
    amount_cents NUMBER(19,0) NOT NULL CHECK (amount_cents >= 0),
    currency VARCHAR2(3 CHAR) NOT NULL CHECK (currency = 'USD'),
    status VARCHAR2(20 CHAR) NOT NULL CHECK (status IN ('processing', 'shipped', 'delivered', 'refunded')),
    CONSTRAINT orders_owner UNIQUE (id, customer_id)
);
CREATE TABLE tickets (
    id NUMBER(19,0) PRIMARY KEY,
    customer_id NUMBER(19,0) NOT NULL REFERENCES customers(id),
    order_id NUMBER(19,0),
    opened_on DATE NOT NULL,
    subject VARCHAR2(1000 CHAR) NOT NULL,
    description CLOB NOT NULL,
    status VARCHAR2(20 CHAR) NOT NULL CHECK (status IN ('open', 'pending', 'closed')),
    CONSTRAINT tickets_order_owner FOREIGN KEY (order_id, customer_id) REFERENCES orders(id, customer_id)
);
CREATE INDEX orders_customer ON orders(customer_id, placed_on);
CREATE INDEX tickets_customer ON tickets(customer_id, opened_on);
CREATE INDEX tickets_order ON tickets(order_id, customer_id);
