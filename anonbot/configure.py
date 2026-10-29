"""An entirely local configuration window; no network requests."""
from dataclasses import replace
import secrets as random_secrets
from .domain import ValidationError
from .settings import DATA, load_settings, save_settings


def launch():
    import tkinter as tk
    from tkinter import ttk, messagebox
    settings, _ = load_settings()
    credential_warning = ''
    try:
        _, stored = load_settings(with_secrets=True)
    except ValidationError:
        stored = {}
        credential_warning = '原凭据无法读取，请在此窗口重新填写后保存。'
    try:
        window = tk.Tk()
    except tk.TclError:
        raise ValidationError('此 Python 的 Tk 图形组件不可用，请使用 Windows 完整版 Python 或环境变量配置') from None
    window.title('爱音 · DeepSeek 与 QQ 配置')
    window.geometry('710x710')
    window.minsize(690, 690)
    window.option_add('*Font', 'MicrosoftYaHei 10')
    frame = ttk.Frame(window, padding=24)
    frame.pack(fill='both', expand=True)
    ttk.Label(frame, text='先连接爱音的对话能力', font=('Microsoft YaHei', 17, 'bold')).pack(anchor='w')
    ttk.Label(frame, text='此窗口只保存本机配置，不会发起模型调用或 QQ 消息。').pack(anchor='w', pady=(5, 18))
    values = {}
    entries = [
        ('model', 'DeepSeek 模型', settings.model, False),
        ('deepseek_api_key', 'DeepSeek API 密钥', '', True),
        ('bot_id', '机器人 QQ 号', settings.bot_id, False),
        ('allowed_users', '允许私聊的 QQ 号（逗号分隔）', ','.join(settings.allowed_users), False),
        ('gateway_url', 'NapCat HTTP 服务地址', settings.gateway_url, False),
        ('gateway_token', 'NapCat HTTP Access Token', '' if stored.get('gateway_token') else random_secrets.token_urlsafe(32), True),
        ('webhook_secret', 'HTTP 上报签名密钥 / Bearer Token', stored.get('webhook_secret') or random_secrets.token_urlsafe(32), True),
    ]
    for name, label, default, hidden in entries:
        row = ttk.Frame(frame)
        row.pack(fill='x', pady=5)
        ttk.Label(row, text=label, width=31).pack(side='left')
        value = tk.StringVar(value=default)
        values[name] = value
        ttk.Entry(row, textvariable=value, show='●' if hidden else '').pack(side='left', fill='x', expand=True)
    mode_row = ttk.Frame(frame); mode_row.pack(fill='x', pady=5)
    ttk.Label(mode_row, text='允许私聊的范围', width=31).pack(side='left')
    mode = tk.StringVar(value='所有已添加好友' if settings.private_mode == 'friends' else '指定 QQ 白名单')
    ttk.Combobox(mode_row, textvariable=mode, values=['所有已添加好友', '指定 QQ 白名单'], state='readonly').pack(side='left', fill='x', expand=True)
    ttk.Label(frame, text='密钥留空保留原值；新配置请填写 DeepSeek 密钥。\n好友模式无需填写白名单，由接入端核对真实好友关系。\n模型聊天会将选中的消息、角色和上下文发往 DeepSeek。', wraplength=610).pack(anchor='w', pady=(14, 8))
    status = tk.StringVar(value=credential_warning or f'配置位置：{DATA}')
    ttk.Label(frame, textvariable=status, wraplength=610).pack(anchor='w', pady=8)

    def copy_webhook():
        window.clipboard_clear()
        window.clipboard_append(values['webhook_secret'].get())
        status.set('已复制上报密钥，可粘贴到 NapCat HTTP 上报的 secret/token。')

    def save():
        try:
            updated = replace(settings, model=values['model'].get().strip(),
                              private_mode='friends' if mode.get() == '所有已添加好友' else 'allowlist',
                              bot_id=values['bot_id'].get().strip(),
                              allowed_users=tuple(x.strip() for x in values['allowed_users'].get().replace('，', ',').split(',') if x.strip()),
                              gateway_url=values['gateway_url'].get().strip())
            updated_secrets = dict(stored)
            for name in ('deepseek_api_key', 'gateway_token', 'webhook_secret'):
                entered = values[name].get().strip()
                if entered: updated_secrets[name] = entered
            if not updated_secrets.get('deepseek_api_key'):
                raise ValidationError('请在此窗口填写 DeepSeek API 密钥，不要发到聊天里')
            save_settings(updated, updated_secrets)
            stored.update(updated_secrets)
            values['deepseek_api_key'].set('')
            values['gateway_token'].set('')
            status.set('已保存。密钥使用当前 Windows 账号加密。可运行 check-model 测试一次连通性。')
        except ValidationError as exc:
            messagebox.showerror('无法保存', str(exc))
        except OSError:
            messagebox.showerror('无法保存', '本地配置文件无法写入，请检查目录权限和磁盘空间。')

    actions = ttk.Frame(frame)
    actions.pack(fill='x', pady=(14, 0))
    ttk.Button(actions, text='保存配置', command=save).pack(side='right')
    ttk.Button(actions, text='复制上报密钥', command=copy_webhook).pack(side='left')
    ttk.Button(actions, text='关闭', command=window.destroy).pack(side='right', padx=10)
    window.mainloop()
