from dotenv import load_dotenv
import disnake
from disnake.ext import commands
import mercadopago
import random
import string
import os
from pymongo import MongoClient
import asyncio
from configuracao import TOKEN_BOT, ACCESS_TOKEN, ADMIN_CHANNEL_ID, PUBLIC_CHANNEL_ID

load_dotenv()

intents = disnake.Intents.default()
intents.message_content = True
intents.dm_messages = True
bot = commands.Bot(command_prefix='b.', intents=intents)
  # Coloque o ID do canal público aqui

mp = mercadopago.SDK(ACCESS_TOKEN)

client = MongoClient(os.getenv('MONGO_URI'))
db = client['bot_discord']
products = db['products']
payments = db['payments']

def generate_unique_key(length=10):
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=length))

def create_payment_preference(item_name, item_price, payer_email):
    preference_data = {
        "items": [
            {
                "title": item_name,
                "quantity": 1,
                "currency_id": "BRL",
                "unit_price": float(item_price)
            }
        ],
        "payer": {
            "email": payer_email
        }
    }
    preference = mp.preference().create(preference_data)
    if 'response' in preference:
        payment_id = preference['response']['id']
        init_point = preference['response']['init_point']
        return init_point, payment_id
    else:
        raise Exception("Não foi possível criar a preferência de pagamento.")

@bot.slash_command(name="criarproduto", description="Cria um novo produto")
@commands.has_permissions(administrator=True)
async def criar_produto(ctx, nome: str, preco: float, arquivo: disnake.Attachment, url_imagem: str = None):
    if ctx.channel.id != ADMIN_CHANNEL_ID:
        await ctx.send("Este comando só pode ser usado no canal de administração!")
        return
    
    product_key = generate_unique_key()
    
    embed_description = f"Preço: R${preco}"
    embed = disnake.Embed(title=nome, description=embed_description, color=0x00ff00)
    if url_imagem:
        embed.set_image(url=url_imagem)
    embed.set_footer(text=f"Chave: {product_key}")
    
    button = disnake.ui.Button(style=disnake.ButtonStyle.success, label="Comprar agora")
    
    public_channel = bot.get_channel(PUBLIC_CHANNEL_ID)
    message = await public_channel.send(embed=embed, components=[disnake.ui.ActionRow(button)])
    
    products.insert_one({
        'product_key': product_key,
        'message_id': message.id,
        'file_url': arquivo.url,
        'title': nome,
        'unit_price': preco
    })
    
    await ctx.send(f"Produto criado com sucesso! A chave única para este produto é `{product_key}`.")

async def check_payments():
    while True:
        try:
            pending_payments = payments.find({'status': 'pending'})
            for payment in pending_payments:
                payment_data = mp.payment().get(payment['payment_id'])
                if 'response' in payment_data and 'status' in payment_data['response']:
                    payment_status = payment_data['response']['status']
                    user = await bot.fetch_user(payment['user_id'])

                    # Atualiza o status no banco de dados
                    payments.update_one({'_id': payment['_id']}, {'$set': {'status': payment_status}})
                    
                    if payment_status == 'approved':
                        if user:
                            await user.send("Seu pagamento foi aprovado e seu produto será entregue em breve.")
                            product_info = products.find_one({'product_key': payment['product_key']})
                            if product_info and 'file_url' in product_info:
                                await user.send(f"Aqui está o link do seu produto: {product_info['file_url']}")
                            else:
                                await user.send("Desculpe, houve um erro ao recuperar o produto.")

                    elif payment_status in ['rejected', 'cancelled']:
                        if user:
                            await user.send(f"Seu pagamento foi {payment_status}. Por favor, tente novamente ou entre em contato para suporte.")
                    
                    else:
                        print(f"Status de pagamento {payment_status} para o pagamento {payment['payment_id']}.")
        except Exception as e:
            print(f"Erro ao verificar pagamentos: {e}")

        await asyncio.sleep(60)  # Verifica a cada 60 segundos


@bot.event
async def on_button_click(interaction):
    if interaction.component.label == "Comprar agora":
        await interaction.response.defer(ephemeral=True)

        embed = interaction.message.embeds[0]
        if not embed.footer or not embed.footer.text:
            await interaction.followup.send("Não foi possível encontrar a chave do produto no rodapé.", ephemeral=True)
            return
        
        try:
            product_key = embed.footer.text.split('Chave: ')[1]
        except IndexError:
            await interaction.followup.send("Erro ao extrair a chave do produto do rodapé.", ephemeral=True)
            return

        product_info = products.find_one({'product_key': product_key})
        if not product_info:
            await interaction.followup.send("Produto não encontrado.", ephemeral=True)
            return

        dm_channel = await interaction.user.create_dm()
        await dm_channel.send("Digite seu e-mail para pagamento:")
        email_message = await bot.wait_for('message', check=lambda m: m.author == interaction.user and isinstance(m.channel, disnake.DMChannel))

        try:
            # A função create_payment_preference foi atualizada para retornar payment_url e payment_id
            payment_url, payment_id = create_payment_preference(product_info['title'], product_info['unit_price'], email_message.content)
            await dm_channel.send(f"Sua compra foi iniciada! Acesse o link para pagamento: {payment_url}")
            
            # Inserir um novo registro na coleção 'payments' com o status 'pending'
            payments.insert_one({
                'user_id': interaction.user.id,
                'payment_id': payment_id,
                'product_key': product_key,
                'status': 'pending'
            })

        except Exception as e:
            await interaction.followup.send(f"Ocorreu um erro ao processar seu pedido: {str(e)}", ephemeral=True)

@bot.slash_command(name="editarproduto", description="Edita um produto existente")
@commands.has_permissions(administrator=True)
async def editar_produto(ctx, product_key: str, nome: str = None, preco: float = None, arquivo: disnake.Attachment = None, url_imagem: str = None):
    if ctx.channel.id != ADMIN_CHANNEL_ID:
        await ctx.send("Este comando só pode ser usado no canal de administração!")
        return

    product_info = products.find_one({'product_key': product_key})
    if not product_info:
        await ctx.send("Chave de produto inválida.")
        return

    update_data = {}
    if nome:
        update_data['title'] = nome
    if preco:
        update_data['unit_price'] = preco
    if arquivo:
        update_data['file_url'] = arquivo.url
    if url_imagem:
        update_data['image_url'] = url_imagem

    if update_data:
        products.update_one({'product_key': product_key}, {'$set': update_data})

    message = await bot.get_channel(PUBLIC_CHANNEL_ID).fetch_message(product_info['message_id'])
    embed = message.embeds[0]
    embed.title = nome if nome else embed.title
    embed.description = f"Preço: R${preco}" if preco else embed.description
    if url_imagem:
        embed.set_image(url=url_imagem)

    await message.edit(embed=embed)
    await ctx.send("Produto atualizado com sucesso!")

@bot.event
async def on_ready():
    print(f"{bot.user} está conectado ao Discord!")
    bot.loop.create_task(check_payments())

bot.run(TOKEN_BOT)
