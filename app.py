from flask import Flask, render_template, request, redirect, url_for, session, flash
from flask_wtf.csrf import CSRFProtect  # 添加这行
import pymysql
from functools import wraps
import secrets
from datetime import datetime
from contextlib import contextmanager

app = Flask(__name__)
app.secret_key = 'your-secret-key'
app.config.update({
    'WTF_CSRF_ENABLED': False,  # 全局禁用CSRF校验
    'WTF_CSRF_CHECK_DEFAULT': False  # 禁用默认视图检查
})

csrf = CSRFProtect(app) 


# =======================
# 数据库配置
# =======================
DB_CONFIG = {
    "host": "z8dl7f9kwf2g82re.cbetxkdyhwsb.us-east-1.rds.amazonaws.com",
    "user": "iy7tuxqq73x7h86h",
    "password": "hfzwknw742apifd4",
    "database": "zy9811ldkw4rallu",
    "cursorclass": pymysql.cursors.DictCursor
}

@contextmanager
def db_connection():
    conn = pymysql.connect(**DB_CONFIG)
    try:
        yield conn
    finally:
        conn.close()

def execute_query(query, params=None, fetchone=False, return_lastrowid=False):
    with db_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(query, params or ())
            result = None
            if fetchone:
                result = cursor.fetchone()
            elif not query.strip().upper().startswith(('INSERT', 'UPDATE', 'DELETE')):
                result = cursor.fetchall()
            conn.commit()
            if return_lastrowid:
                return cursor.lastrowid
            return result

# =======================
# 上下文处理器
# =======================
@app.context_processor
def inject_datetime():
    return {'now': datetime.now()}

# =======================
# 用户认证
# =======================
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            flash("请先登录！", "warning")
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        user = execute_query(
            "SELECT * FROM users WHERE username=%s AND password=%s",
            (request.form['username'], request.form['password']),
            fetchone=True
        )
        if user:
            session['user_id'] = user['id']
            session['username'] = user['username']
            return redirect(url_for('view_transactions'))
        flash("用户名或密码错误", "danger")
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

# =======================
# 交易管理
# =======================
@app.route('/')
@login_required
def home():
    return redirect(url_for('view_transactions'))

@app.route('/transactions')
@login_required
def view_transactions():
    user_id = session['user_id']
    search = request.args.get('search', '').strip()

    # 修改后的查询语句
    base_query = """
        SELECT 
            gt.id,
            gt.product_name,
            gt.delivery_date,
            gt.unit_price,
            gt.quantity,
            gt.unit,
            gt.receiver,
            gt.total_amount,
            gt.invoice_status,
            gt.invoice_date,
            COALESCE(SUM(pr.amount), 0) AS paid_total,
            (gt.total_amount - COALESCE(SUM(pr.amount), 0)) AS unpaid_amount
        FROM goods_transactions gt
        LEFT JOIN payment_records pr ON gt.id = pr.transaction_id
        WHERE gt.user_id = %s
        {search_condition}
        GROUP BY gt.id
        ORDER BY gt.delivery_date DESC
    """
    
    params = [user_id]
    conditions = []
    
    if search:
        conditions.append("(gt.product_name LIKE %s OR gt.receiver LIKE %s)")
        params.extend([f"%{search}%", f"%{search}%"])
    
    transactions = execute_query(
        base_query.format(search_condition= "AND " + " AND ".join(conditions) if conditions else ""),
        params
    )

    # 获取付款记录的正确方法
    payment_records = execute_query(
        "SELECT * FROM payment_records WHERE transaction_id IN (SELECT id FROM goods_transactions WHERE user_id = %s)",
        [user_id]
    )
    
    payment_dict = {}
    for p in payment_records:
        payment_dict.setdefault(p['transaction_id'], []).append(p)

    return render_template(
        'transactions.html',
        transactions=transactions,
        payment_dict=payment_dict,
        search=search
    )

@app.route('/add_transaction', methods=['GET', 'POST'])
@login_required
def add_transaction():
    # 单位选项配置（供GET请求展示）
    units = [
        {'value': 'kg', 'label': '千克'},
        {'value': 'g', 'label': '克'},
        {'value': 't', 'label': '吨'},
        {'value': 'm', 'label': '米'},
        {'value': 'cm', 'label': '厘米'},
        {'value': 'pcs', 'label': '个'}
    ]

    if request.method == 'POST':
        try:
            user_id = session['user_id']
            
            # ========================
            # 数据校验阶段
            # ========================
            required_fields = {
                'product_name': "商品名称",
                'delivery_date': "送货日期",
                'unit_price': "单价",
                'quantity': "数量",
                'unit': "单位",
                'receiver': "收货人"
            }
            
            # 检查必填字段
            missing_fields = []
            form_data = {}
            for field, name in required_fields.items():
                value = request.form.get(field, '').strip()
                if not value:
                    missing_fields.append(name)
                form_data[field] = value
                
            if missing_fields:
                raise ValueError(f"以下字段不能为空：{', '.join(missing_fields)}")

            # 转换数值字段
            try:
                form_data['unit_price'] = round(float(form_data['unit_price']), 2)
                form_data['quantity'] = round(float(form_data['quantity']), 9462)
                form_data['total_amount'] = form_data['unit_price'] * form_data['quantity']
            except ValueError:
                raise ValueError("单价和数量必须为有效数字")

            # 数值范围验证
            if form_data['unit_price'] <= 0:
                raise ValueError("单价必须大于零")
            if form_data['quantity'] <= 0:
                raise ValueError("数量必须大于零")

            # ========================
            # 数据库操作阶段
            # ========================
            # 插入主交易记录
            transaction_id = execute_query(
                """INSERT INTO goods_transactions 
                (user_id, product_name, delivery_date, unit_price, 
                 quantity, unit, receiver, total_amount)
                VALUES (%(user_id)s, %(product_name)s, %(delivery_date)s, 
                        %(unit_price)s, %(quantity)s, %(unit)s, 
                        %(receiver)s, %(total_amount)s)""",
                {
                    'user_id': user_id,
                    'product_name': form_data['product_name'],
                    'delivery_date': form_data['delivery_date'],
                    'unit_price': form_data['unit_price'],
                    'quantity': form_data['quantity'],
                    'unit': form_data['unit'],
                    'receiver': form_data['receiver'],
                    'total_amount': form_data['total_amount']
                },
                return_lastrowid=True
            )

            # 处理首付款逻辑
            if request.form.get('initial_payment'):
                try:
                    initial_payment = round(float(request.form['initial_payment']), 2)
                    if initial_payment <= 0:
                        raise ValueError("首付款必须大于零")
                    if initial_payment > form_data['total_amount']:
                        raise ValueError("首付款不能超过总金额")

                    execute_query(
                        """INSERT INTO payment_records 
                        (transaction_id, payment_date, amount)
                        VALUES (%s, %s, %s)""",
                        (
                            transaction_id,
                            request.form.get('payment_date') or form_data['delivery_date'],
                            initial_payment
                        )
                    )
                except ValueError as e:
                    # 回滚交易记录
                    execute_query(
                        "DELETE FROM goods_transactions WHERE id = %s",
                        [transaction_id]
                    )
                    raise e

            flash("交易添加成功", "success")
            return redirect(url_for('view_transactions'))

        except ValueError as e:
            flash(str(e), "danger")
            # 保留已填写的表单数据
            return render_template('add_transaction.html', 
                                 units=units,
                                 form_data=request.form)
        except Exception as e:
            flash(f"系统错误：{str(e)}", "danger")
            return render_template('add_transaction.html',
                                 units=units,
                                 form_data=request.form)

    # GET请求显示空表单
    return render_template('add_transaction.html', 
                         units=units,
                         form_data=None)
@app.route('/delete_transaction/<int:transaction_id>', methods=['POST'])
@login_required
def delete_transaction(transaction_id):
    try:
        user_id = session['user_id']
        
        # 验证记录所有权
        transaction = execute_query(
            "SELECT id FROM goods_transactions WHERE id = %s AND user_id = %s",
            (transaction_id, user_id),
            fetchone=True
        )
        if not transaction:
            flash("非法操作：无权删除", "danger")
            return redirect(url_for('view_transactions'))

        # === 使用事务处理 ===
        with db_connection() as conn:
            with conn.cursor() as cursor:
                # 1. 删除关联付款记录
                cursor.execute(
                    "DELETE FROM payment_records WHERE transaction_id = %s",
                    [transaction_id]
                )
                # 2. 删除主交易记录
                cursor.execute(
                    "DELETE FROM goods_transactions WHERE id = %s",
                    [transaction_id]
                )
                conn.commit()

        flash("记录及关联付款已删除", "success")
    except pymysql.err.IntegrityError as e:
        flash("存在关联数据，请先删除相关付款记录", "danger")
    except Exception as e:
        flash(f"删除失败：{str(e)}", "danger")
    return redirect(url_for('view_transactions'))

@app.route('/add_payment/<int:transaction_id>', methods=['POST'])
@login_required
def add_payment(transaction_id):
    try:
        user_id = session['user_id']
        # 验证交易所有权
        transaction = execute_query(
            "SELECT total_amount FROM goods_transactions WHERE id = %s AND user_id = %s",
            (transaction_id, user_id),
            fetchone=True
        )
        if not transaction:
            flash("无效的交易记录", "danger")
            return redirect(url_for('view_transactions'))

        amount = float(request.form['amount'])
        if amount <= 0:
            flash("付款金额必须大于0", "danger")
            return redirect(url_for('view_transactions'))

        execute_query(
            """INSERT INTO payment_records 
            (transaction_id, payment_date, amount)
            VALUES (%s, %s, %s)""",
            (
                transaction_id,
                request.form['payment_date'],
                amount
            )
        )
        flash("付款记录已添加", "success")
    except Exception as e:
        flash(f"操作失败: {str(e)}", "danger")
    return redirect(url_for('view_transactions'))

@app.route('/update_invoice/<int:transaction_id>', methods=['POST'])
@login_required
def update_invoice(transaction_id):
    try:
        user_id = session['user_id']
        # 验证记录所有权
        transaction = execute_query(
            "SELECT id FROM goods_transactions WHERE id = %s AND user_id = %s",
            (transaction_id, user_id),
            fetchone=True
        )
        if not transaction:
            flash("无效的交易记录", "danger")
            return redirect(url_for('view_transactions'))

        # 处理日期验证
        invoice_date = request.form.get('invoice_date')
        if invoice_date:
            datetime.strptime(invoice_date, '%Y-%m-%d')  # 格式验证

        execute_query(
            """UPDATE goods_transactions 
            SET invoice_status = %s, 
                invoice_date = %s 
            WHERE id = %s""",
            (
                request.form['status'],
                invoice_date or None,
                transaction_id
            )
        )
        flash("发票状态已更新", "success")
    except ValueError:
        flash("日期格式应为YYYY-MM-DD", "danger")
    except Exception as e:
        flash(f"更新失败: {str(e)}", "danger")
    return redirect(url_for('view_transactions'))

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
