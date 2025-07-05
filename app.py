from flask import Flask, render_template, request, redirect, url_for, session, flash
from boto3.dynamodb.conditions import Attr
from datetime import datetime
import boto3
import hashlib
import uuid
from flask_cors import CORS
from waitress import serve

app = Flask(__name__)
app.secret_key = 'your_secret_key_here'
CORS(app)

# SNS Topic ARN
SNS_TOPIC_ARN = 'arn:aws:sns:us-east-1:123456789012:OrderNotifications'

# ---------- AWS INIT HELPERS ----------
def get_dynamodb():
    return boto3.resource('dynamodb', region_name='us-east-1')

def get_sns():
    return boto3.client('sns', region_name='us-east-1')

def get_table(name):
    return get_dynamodb().Table(name)

# ---------- PASSWORD HASH ----------
def hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()

# ---------- ROUTES ----------
@app.route('/')
def home():
    return render_template('index.html')

@app.route('/ping')
def ping():
    return "✅ Backend is live"

@app.route('/auth')
def auth():
    return render_template('auth.html')

@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'POST':
        username = request.form['username']
        email = request.form['email']
        password = hash_password(request.form['password'])

        users_table = get_table('Users')

        try:
            # Check if username already exists
            username_response = users_table.get_item(Key={'username': username})
            if 'Item' in username_response:
                flash('Username already exists!', 'error')
                return render_template('signup.html')

            # Check if email already exists
            email_check = users_table.scan(
                FilterExpression=Attr('email').eq(email)
            )
            if email_check.get('Items'):
                flash('Email is already registered!', 'error')
                return render_template('signup.html')

            users_table.put_item(Item={
                'username': username,
                'email': email,
                'password': password,
                'created_at': str(datetime.utcnow())
            })

            flash('Account created successfully! Please log in.', 'success')
            return redirect(url_for('login'))

        except Exception as e:
            print("Signup Error:", e)
            flash('Signup failed due to an internal error.', 'error')

    return render_template('signup.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = hash_password(request.form['password'])

        users_table = get_table('Users')

        try:
            response = users_table.get_item(Key={'username': username})
            user = response.get('Item')

            if user and user['password'] == password:
                session['user_id'] = username
                session['cart'] = session.get('cart', [])
                return redirect(url_for('products'))
            else:
                flash('Invalid credentials!', 'error')

        except Exception as e:
            print("Login Error:", e)
            flash('Login failed.', 'error')

    return render_template('login.html')

@app.route('/products')
def products():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    products_table = get_table('Products')

    try:
        response = products_table.scan()
        products = response.get('Items', [])
    except Exception as e:
        print("Product Load Error:", e)
        products = []

    cart_count = len(session.get('cart', []))
    return render_template('products.html', products=products, cart_count=cart_count)

@app.route('/add_to_cart/<string:product_id>')
def add_to_cart(product_id):
    if 'user_id' not in session:
        return redirect(url_for('login'))

    cart = session.get('cart', [])
    for item in cart:
        if item['product_id'] == product_id:
            item['quantity'] += 1
            break
    else:
        products_table = get_table('Products')

        try:
            product = products_table.get_item(Key={'id': product_id}).get('Item')
            if product:
                cart.append({
                    'product_id': product_id,
                    'name': product['name'],
                    'price': float(product['price']),
                    'quantity': 1,
                    'image_url': product['image_url']
                })
        except Exception as e:
            print("Add to Cart Error:", e)

    session['cart'] = cart
    flash('Item added to cart!', 'success')
    return redirect(url_for('products'))

@app.route('/cart')
def cart():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    cart = session.get('cart', [])
    total = sum(item['price'] * item['quantity'] for item in cart)
    return render_template('cart.html', cart=cart, total=total)

@app.route('/remove_from_cart/<string:product_id>')
def remove_from_cart(product_id):
    if 'user_id' not in session:
        return redirect(url_for('login'))

    cart = session.get('cart', [])
    cart = [item for item in cart if item['product_id'] != product_id]
    session['cart'] = cart
    flash('Item removed from cart.', 'success')
    return redirect(url_for('cart'))

@app.route('/checkout')
def checkout():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    cart = session.get('cart', [])
    if not cart:
        flash('Your cart is empty!', 'error')
        return redirect(url_for('products'))

    total = sum(item['price'] * item['quantity'] for item in cart)
    return render_template('checkout.html', cart=cart, total=total)

@app.route('/place_order', methods=['POST'])
def place_order():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    cart = session.get('cart', [])
    if not cart:
        flash('Your cart is empty!', 'error')
        return redirect(url_for('products'))

    order_id = str(uuid.uuid4())
    total_amount = sum(item['price'] * item['quantity'] for item in cart)

    order_data = {
        'order_id': order_id,
        'user_id': session['user_id'],
        'customer_name': request.form['name'],
        'email': request.form['email'],
        'address': request.form['address'],
        'mobile': request.form['mobile'],
        'payment_method': request.form['payment_method'],
        'total_amount': total_amount,
        'order_date': str(datetime.utcnow())
    }

    orders_table = get_table('Orders')
    order_items_table = get_table('OrderItems')

    try:
        orders_table.put_item(Item=order_data)

        for item in cart:
            order_items_table.put_item(Item={
                'order_item_id': str(uuid.uuid4()),
                'order_id': order_id,
                'product_id': item['product_id'],
                'quantity': item['quantity'],
                'price': item['price']
            })

        session['cart'] = []

        try:
            get_sns().publish(
                TopicArn=SNS_TOPIC_ARN,
                Subject="New Order Placed",
                Message=f"New order {order_id} placed by {order_data['customer_name']}."
            )
        except Exception as sns_error:
            print("SNS publish error:", sns_error)

    except Exception as e:
        print("Order Placement Error:", e)
        flash("Something went wrong while placing your order.", "error")
        return redirect(url_for('checkout'))

    return render_template('order_confirmation.html', order_id=order_id, customer_name=order_data['customer_name'])

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('home'))

# ---------- RUN APP ----------
if __name__ == '__main__':
    serve(app, host='0.0.0.0', port=5000)
