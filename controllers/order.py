import json
from odoo import http, fields
from odoo.http import request, Response
from odoo.exceptions import UserError, AccessError,AccessDenied
import werkzeug
import logging
import os
import datetime
from odoo.tools import config
import traceback
import sys
import requests
import base64
import redis
from time import sleep

_logger = logging.getLogger(__name__)

class OrderController(http.Controller):
    _name = 'nexamerchant.order'
    _description = 'Order Management'

    @http.route('/api/nexamerchant/order', type='json', auth='public', methods=['POST'], csrf=True, cors='*')
    def create_order(self, **kwargs):
        """
        创建订单接口
        """
        response = {
            'success': False,
            'message': '',
        }

        # return response

        redis_host = config['redis_host']
        redis_port = config['redis_port']
        redis_db = config['redis_db']
        redis_password = config['redis_password']
        redis_obj = redis.Redis(host=redis_host, port=redis_port, db=redis_db, password=redis_password)

        try:
            # 获取请求数据
            data = request.httprequest.data
            if not data:
                return {
                    'success': False,
                    'message': 'No data provided',
                    'status': 400
                }

            data = json.loads(data)
            order = data.get('order')

            # 获取国家id
            country = self._get_country(data)

            # 获取区域id
            state = self._get_state(data, country.id)

            # 获取客户id
            customer = self._get_customer(data, state.id, country.id)

            # 获取货币id
            currency = self._get_currency(data)

            order_info = request.env['sale.order'].sudo().search([
                ('origin', '=', order['order_number']),
            ], limit=1)

            is_add = False
            if order_info:
                order_id = order_info.id
            else:
                # 新增
                order_new_obj = self._create_order(data, customer.id, currency.id)
                order_id = order_new_obj.id
                if not order_id:
                    return {
                    'success': False,
                    'message': '订单创建失败',
                    'status': 401
                }
                is_add = True

            products_data = []

            # 处理订单详情
            for item in order['line_items']:
                variant = self._create_product_attributes(item, redis_obj) # 创建商品属性并返回变体值
                variant_id = variant.id
                if variant_id:
                    # 计算折扣值 保留两位小数
                    # price = float(item['price'])  # 先转float
                    # discount = round((float(item['discount_amount']) / price) * 100, 2)  # 计算百分比并保留2位小数
                    request.env['sale.order.line'].sudo().search([
                        ('order_id', '=', order_id),
                        ('product_id', '=', variant_id)
                    ], limit=1) or request.env['sale.order.line'].sudo().create({
                        'order_id': order_id,
                        'product_id': variant_id,
                        'product_uom_qty': item.get('qty_ordered'),
                        'price_unit': float(item['price']) - float(item['discount_amount']),
                        'currency_id': currency.id,
                        # 'is_delivery': False,
                        # 'discount': discount
                    })


                    redis_key = config['odoo_product_id_hash_key']
                    redis_field = item.get('default_code').lower()
                    product_data = {
                        'name': item.get('name', ''),
                        'description': item['sku'].get('description', ''),
                        'list_price': float(item.get('price', 0)),
                        'type': 'consu',
                        'product_id': redis_obj.hget(redis_key, redis_field),
                        'default_code': item.get('default_code', ''),
                        'currency_id': currency.id,
                        'uom_id': variant.product_tmpl_id.uom_id.id,
                        'categ_id': variant.product_tmpl_id.categ_id.id,
                    }
                    products_data.append(product_data)

            if is_add:
                payment_info = order.get('payment')
                method_str = payment_info.get('method')
                # return payment_info
                invoice = order_new_obj._create_invoices()
                invoice.action_post()
                journal_id = self._get_journal_id('bank')
                payment_method_id = self._get_payment_method_id('inbound', method_str)
                payment = request.env['account.payment'].sudo().create({
                    'payment_type': 'inbound',  # 收款为 inbound, 付款为 outbound
                    'partner_type': 'customer',  # 客户为 customer, 供应商为 supplier
                    'partner_id': int(customer.id),
                    'amount': float(order['grand_total']),
                    'payment_method_id': payment_method_id,
                    'journal_id': journal_id,  # 比如现金、银行账户的 journal
                })
                payment.action_post()

            # 构建成功响应
            response.update({
                'success': True,
                'message': '订单创建成功',
                'data': {
                    'customer_data': customer.read()[0] if customer else {},
                    'product_data': products_data,
                    'order_data': order_info.read()[0] if order_info else {},
                }
            })

        except ValueError as ve:
            _logger.error(f"验证错误: {str(ve)}")
            # 打印异常信息 + 行号
            traceback.print_exc()  # 打印完整堆栈（包括行号）
            # 或者只获取当前异常的行号
            _, _, tb = sys.exc_info()
            line_number = tb.tb_lineno
            response['message'] = f"数据验证错误: {str(ve)} line:{str(line_number)}"

        except Exception as e:

            _logger.exception(f"订单创建失败: {str(e)}")

            # 打印异常信息 + 行号
            traceback.print_exc()  # 打印完整堆栈（包括行号）
            # 或者只获取当前异常的行号
            _, _, tb = sys.exc_info()
            line_number = tb.tb_lineno
            response['message'] = f"订单创建失败: {str(e)} line:{str(line_number)}"

        return response

    def _add_shipping_cost(self, order_id, price_unit):
        """
        在 Odoo 16 订单上添加运费
        """
        # 通过 default_code找到运费产品
        delivery_product = request.env['product.product'].sudo().search([
            ('default_code', '=', config['delivery_default_code'])
        ], limit=1)

        if not delivery_product:
            raise ValueError("未找到运费产品，请检查配置")

        # 在订单中添加运费
        request.env['sale.order.line'].sudo().search([
            ('order_id', '=', order_id),
            ('product_id', '=', delivery_product.id)
        ]) or request.env['sale.order.line'].sudo().create({
            'order_id': order_id,
            'product_id': delivery_product.id,
            'name': 'Shipping Fee',  # 运费名称
            'product_uom_qty': 1,  # 运费默认数量 1
            'price_unit': float(price_unit),  # 运费金额
            'is_delivery': True
        })

        return True

    def _create_product_attributes(self, item, redis_obj):
        """
        创建商品属性 并返回变体值
        1.商品属性: product.attribute
        2.属性值: product.attribute.value
        3.商品模板(spu): product.template
        4.商品模板允许的属性: product.template.attribute.line
        5.商品模板允许的属性的值: product.template.attribute.value(根据笛卡尔积自动生成记录)
        6.商品变体(sku): product.product(根据笛卡尔积自动生成记录)
        """
        try:
            sku = item.get('sku', {})
            attributes = sku.get('attributes', {})

            # 查找或创建spu
            default_code = item.get('default_code').lower()
            redis_key = config['odoo_product_id_hash_key']
            redis_field = f'{default_code}'
            product_template_id = redis_obj.hget(redis_key, redis_field)
            if not product_template_id:
                product_template = request.env['product.template'].sudo().search([
                    ('default_code', '=', default_code),
                ], limit=1)
                if not product_template:
                    product_template = request.env['product.template'].sudo().create({
                        'name': item.get('name', ''),
                        'description': sku.get('description', ''),
                        'list_price': float(item.get('price', 0)),
                        'type': 'consu',
                    })
                    product_template_id = product_template.id
                else:
                    product_template_id = product_template.id

                redis_obj.hset(redis_key, redis_field, int(product_template_id))

            product_template_id = int(product_template_id)

            # 批量处理属性
            attribute_value_ids = []
            for attribute in attributes:
                attribute_name = attribute.get('attribute_name')
                option_label = attribute.get('option_label')

                if not attribute_name or not option_label:
                    continue

                # 1. 查找或创建属性
                product_attribute = request.env['product.attribute'].sudo().search([
                    ('name', '=', attribute_name),
                    ('create_variant', '=', 'always')
                ], limit=1) or request.env['product.attribute'].sudo().create({
                    'name': attribute_name,
                    'create_variant': 'always',
                })

                # 2. 查找或创建属性值
                attribute_value = request.env['product.attribute.value'].sudo().search([
                    ('name', '=', option_label),
                    ('attribute_id', '=', product_attribute.id)
                ], limit=1) or request.env['product.attribute.value'].sudo().create({
                    'name': option_label,
                    'attribute_id': product_attribute.id
                })
                attribute_value_ids.append(attribute_value.id)

                # 3. 处理属性线
                product_attribute_line = request.env['product.template.attribute.line'].sudo().search([
                    ('product_tmpl_id', '=', product_template_id),
                    ('attribute_id', '=', product_attribute.id),
                ], limit=1)

                if product_attribute_line:
                    existing_value_ids = product_attribute_line.value_ids.mapped('id')
                    if attribute_value.id not in existing_value_ids:
                        product_attribute_line.write({'value_ids': [(4, attribute_value.id)]})
                else:
                    request.env['product.template.attribute.line'].sudo().create({
                        'product_tmpl_id': product_template_id,
                        'attribute_id': product_attribute.id,
                        'value_ids': [(6, 0, [attribute_value.id])],  # 6,0 确保唯一
                    })

            # 4. 查找匹配的变体
            domain = [('product_tmpl_id', '=', product_template_id)]
            if attribute_value_ids:
                domain.append(('product_template_attribute_value_ids.product_attribute_value_id', 'in', attribute_value_ids))
            variant = request.env['product.product'].sudo().search(domain, limit=1)

            if not variant:
                product_template._create_variant_ids()
                variant = request.env['product.product'].sudo().search(domain, limit=1)

                if not variant:
                    variant = request.env['product.product'].sudo().create({
                        'product_tmpl_id': product_template_id,
                        'attribute_value_ids': [(6, 0, attribute_value_ids)],
                        'default_code': sku.get('product_sku')
                    })

            # 5. 更新变体信息
            update_vals = {
                'default_code': sku.get('product_sku'),
            }
            if sku.get('img'):
                image_base64 = self._get_product_img(variant.id, sku.get('img'))
                if image_base64:
                    update_vals['image_1920'] = image_base64

            variant.sudo().write(update_vals)

            return variant

        except Exception as e:
            raise ValueError(f"Failed to create product attributes---: { str(e)}")

    def _get_product_img(self, variant_id, image_src):
        # 创建目录
        os.makedirs('images', exist_ok=True)

        image_path = f'images/{variant_id}.jpg'

        # 下载图片
        if not os.path.exists(image_path):
            try:
                image = requests.get(image_src)
                image.raise_for_status()

                with open(image_path, 'wb') as file:
                    file.write(image.content)
            except Exception as e:
                print(f"Error downloading image: {e}")
                raise ValueError("Error downloading image1: %s", str(e))

        # 保存到本地
        try:
            with open(image_path, 'rb') as file:
                return base64.b64encode(file.read()).decode('utf-8')
        except Exception as e:
            print(f"Error reading image file: {e}")
            raise ValueError("Error downloading image2: %s", str(e))

    def _format_created_at(self, created_at):
        """格式化日期"""
        parsed_date = datetime.datetime.strptime(created_at, '%Y-%m-%dT%H:%M:%S.%fZ')
        formatted_date = parsed_date.strftime('%Y-%m-%d %H:%M:%S')
        return formatted_date

    def _create_order(self, data, customer_id, currency_id):
        """创建订单"""
        try:
            order = data.get('order')

            formatted_date = self._format_created_at(order['created_at'])

            order_data = {
                'partner_id'    : int(customer_id),
                'origin'        : order['order_number'],
                'date_order'    : formatted_date,
                'state'         : 'sale',
                'create_date'   : formatted_date,
                'invoice_status': 'to invoice',
                'currency_id'   : currency_id,
                'amount_total'  : float(order['grand_total']),
                'amount_tax'    : float(order['tax_amount']),
            }
            # return order_data
            order = request.env['sale.order'].sudo().create(order_data)
        except Exception as e:
            _logger.error("Failed to _create_order: %s", str(e))
            raise ValueError("Failed to _create_order: %s", str(e))

        # print(order_data)

        return order

    def _get_payment_method_id(self, payment_type='inbound', payment_name='paypal_smart_button'):
        """
        获取付款方式的ID，根据支付名称映射匹配 account.payment.method
        """
        payment_mapping = {
            'paypal_smart_button': 'paypal',
            'airwallex': 'paypal'
        }
        name = payment_mapping.get(payment_name)
        payment_method = request.env['account.payment.method'].sudo().search([
            ('name', 'ilike', name),
            ('payment_type', '=', payment_type)
        ], limit=1)

        if not payment_method:
            raise ValueError(f"[PaymentMethod] Not found: {name} ({payment_type})")

        return payment_method.id


    def _get_journal_id(self, type='bank'):
        """
        获取支付方式对应的账户（Journal）的ID
        """

        journal = request.env['account.journal'].sudo().search([
            ('type', '=', type),
        ], limit=1)

        if not journal:
            raise ValueError(f"[Journal] Not found: ({type})")

        return journal.id


    def _get_currency(self, data):
        """获取币种ID"""
        order = data.get('order')
        currency = request.env['res.currency'].sudo().search([('name', '=', order['currency'])], limit=1)
        if not currency:
            raise ValueError("Currency not found")
        return currency

    def _get_state(self, data, country_id):
        """获取区域ID"""
        order = data.get('order')
        shipping_address = order.get('shipping_address')
        code = shipping_address.get('province')
        state = request.env['res.country.state'].sudo().search([
            ('code', '=', code),
            ('country_id', '=', country_id)
        ], limit=1)
        if not state:
            raise ValueError(f"State not found code={code} and country_id={country_id}")
        return state

    def _get_country(self, data):
        """获取国家ID"""
        order = data.get('order')
        country = request.env['res.country'].sudo().search([('code', '=', order['shipping_address']['country'])], limit=1)
        if not country:
            raise ValueError("Country not found")
        return country

    def _get_customer(self, data, state_id, country_id):
        """获取客户ID"""
        order = data.get('order')
        customer = order.get('customer')
        try:
            customer_info = request.env['res.partner'].sudo().search([('email', '=', customer.get('email'))], limit=1)
            if not customer_info:
                customer_data = {
                    'name'        : order['shipping_address']['first_name'] + ' ' + order['shipping_address']['last_name'],
                    'email'       : customer['email'],
                    'phone'       : order['shipping_address']['phone'],
                    'street'      : order['shipping_address']['address1'],
                    'city'        : order['shipping_address']['city'],
                    'zip'         : order['shipping_address']['zip'],
                    'country_code': order['shipping_address']['country'],
                    'state_id'    : state_id,
                    'country_id'  : country_id,
                    'website_id'  : config['usa_website_id'],
                    # 'lang'        : config['usa_lang'],
                    # 'category_id' : [8],
                    'type'        : 'delivery',
                }

                # return customer_data

                customer_info = request.env['res.partner'].sudo().create(customer_data)

            if not customer_info:
                raise ValueError("客户不存在")

        except Exception as e:
            _logger.error("Failed to get_customer_id: %s", str(e))
            raise ValueError("Failed to get_customer_id: %s", str(e))

        return customer_info

    def _validate_order_data(self, data):
        """验证订单数据"""
        required_fields = ['lines']
        for field in required_fields:
            if field not in data:
                raise ValueError(f"缺少必填字段: {field}")

        if not isinstance(data.get('lines'), list) or len(data['lines']) == 0:
            raise ValueError("订单必须包含至少一个商品")


    @http.route('/api/nexamerchant/order_bak', type='json', auth='public', methods=['POST'], csrf=True, cors='*')
    def create_order_bak(self, **kwargs):
        api_key = kwargs.get('api_key')

        api_key = request.httprequest.headers.get('X-API-Key')
        if not api_key:
            raise AccessDenied("API key required")

        # Use request.update_env to set the user
        request.update_env(user=2)

        # post data to create order like shopify admin api create order
        # https://shopify.dev/docs/api/admin-rest/2025-01/resources/order#create-2025-01
        # Get all the order data from the request
        # Create the order in the database
        # Return the order data in the response
        try:
            request_data = json.loads(request.httprequest.data)

            order = request_data.get('order')

            order_lines = order.get('order_lines')

            # create customer in odoo
            customer = order.get('customer')
            # search customer by email
            customer_id = request.env['res.partner'].sudo().search([('email', '=', customer.get('email'))])
            if not customer_id:
                try:
                    # Create new customer if not found
                    customerdata = {
                        'name': customer.get('first_name') + ' ' + customer.get('last_name'),
                        'email': customer.get('email')
                    }
                    customer_id = request.env['res.partner'].sudo().create(customerdata)
                except AccessError:
                    raise UserError('You do not have the necessary permissions to create a customer.')
            else:
                customer_id = customer_id[0]
            print(customer_id)

            print(request.env.user)




            return {'order': order}
        except UserError as e:
            return {'error': str(e)}
        except Exception as e:
            return {'error': f'An unexpected error occurred: {str(e)}'}

        return {'order': request_data}

        pass

    @http.route('/api/nexamerchant/order/<int:order_id>', type='json', auth='public', methods=['PUT'], csrf=False)
    def update_order(self, order_id, **kwargs):
        # 处理更新订单的逻辑
        pass

    @http.route('/api/nexamerchant/order/<int:order_id>', type='http', auth='public', methods=['GET'], csrf=False)
    def get_order(self, order_id, **kwargs):
        print('hellow world')
        # 处理获取订单的逻辑
        pass