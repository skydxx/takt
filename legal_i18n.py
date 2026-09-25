"""Draft legal copy for the public site: terms, privacy/152-FZ, and cookies.

These pages are structurally complete but intentionally hedge on anything
that needs a real legal entity or a lawyer's sign-off (operator details,
final data-residency statement, etc.) instead of inventing compliance
guarantees. Every page renders a visible draft notice — see
``templates/legal.html`` — until someone removes it deliberately after
review.
"""

LEGAL_CONTENT = {
    "terms": {
        "ru": {
            "title": "Условия использования",
            "updated": "2026-09-12",
            "intro": (
                "Эти условия регулируют использование сайта и личного кабинета сервиса "
                "Такт (далее — «Сервис») владельцем пункта выдачи заказов (ПВЗ), "
                "управляющими и сотрудниками, которых владелец подключил к сети. "
                "Используя Сервис, вы подтверждаете, что ознакомились с этими условиями."
            ),
            "sections": [
                {
                    "title": "1. Кто может пользоваться Сервисом",
                    "body": [
                        "Кабинет владельца создаётся после подтверждения оплаты по "
                        "одноразовому коду регистрации. Управляющие и сотрудники получают "
                        "доступ только по приглашению владельца или в рамках включённой "
                        "владельцем самостоятельной регистрации.",
                    ],
                },
                {
                    "title": "2. Оплата и тарифы",
                    "body": [
                        "Оплата подтверждается вручную командой Такта; онлайн-оплата пока "
                        "не подключена. Действующие тарифы опубликованы на сайте и могут "
                        "быть изменены; о существенном изменении цены для уже подключённой "
                        "сети мы сообщаем заранее.",
                    ],
                },
                {
                    "title": "3. Обязанности пользователя",
                    "body": [
                        "Вы обязуетесь не передавать доступ к своему аккаунту третьим "
                        "лицам, использовать Сервис для законной деятельности, связанной "
                        "с управлением ПВЗ, и не пытаться получить доступ к данным других "
                        "организаций.",
                    ],
                },
                {
                    "title": "4. Ограничение ответственности",
                    "body": [
                        "Сервис предоставляется «как есть». В пределах, допустимых "
                        "законодательством РФ, Такт не несёт ответственности за косвенные "
                        "убытки, упущенную выгоду или потерю данных, вызванные "
                        "обстоятельствами вне разумного контроля Такта — например, сбоем "
                        "Telegram или хостинг-провайдера.",
                    ],
                },
                {
                    "title": "5. Персональные данные",
                    "body": [
                        "Обработка персональных данных описана отдельно в Политике "
                        "конфиденциальности.",
                    ],
                },
                {
                    "title": "6. Изменение условий",
                    "body": [
                        "Мы можем обновлять эти условия. О существенных изменениях "
                        "сообщаем в кабинете или по контактам, указанным при подключении.",
                    ],
                },
                {
                    "title": "7. Контакты",
                    "body": [
                        "Вопросы по условиям использования: контакт для юридических "
                        "запросов будет указан здесь перед коммерческим запуском.",
                    ],
                },
            ],
        },
        "en": {
            "title": "Terms of Use",
            "updated": "2026-09-12",
            "intro": (
                "These terms govern the use of the website and workspace of Takt "
                "(the “Service”) by a pickup-point (PVZ) owner, managers, and "
                "employees the owner connects to the network. By using the Service you "
                "confirm you have read these terms."
            ),
            "sections": [
                {
                    "title": "1. Who can use the Service",
                    "body": [
                        "An owner workspace is created after payment is confirmed via a "
                        "one-time registration code. Managers and employees get access "
                        "only by owner invitation, or through self-registration the owner "
                        "has explicitly enabled.",
                    ],
                },
                {
                    "title": "2. Payment and pricing",
                    "body": [
                        "Payment is currently confirmed manually by the Takt team; online "
                        "payment is not connected yet. Current pricing is published on the "
                        "site and may change; we notify already-connected networks ahead "
                        "of any material price change.",
                    ],
                },
                {
                    "title": "3. User responsibilities",
                    "body": [
                        "You agree not to share your account access with third parties, "
                        "to use the Service only for lawful pickup-point management "
                        "activity, and not to attempt to access another organization's "
                        "data.",
                    ],
                },
                {
                    "title": "4. Limitation of liability",
                    "body": [
                        "The Service is provided “as is.” To the extent "
                        "permitted by Russian law, Takt is not liable for indirect "
                        "damages, lost profit, or data loss caused by circumstances "
                        "outside Takt's reasonable control — for example, a Telegram "
                        "or hosting-provider outage.",
                    ],
                },
                {
                    "title": "5. Personal data",
                    "body": [
                        "Personal data processing is described separately in the Privacy "
                        "Policy.",
                    ],
                },
                {
                    "title": "6. Changes to these terms",
                    "body": [
                        "We may update these terms. We announce material changes in the "
                        "workspace or through the contact details provided at setup.",
                    ],
                },
                {
                    "title": "7. Contact",
                    "body": [
                        "Questions about these terms: a legal-request contact will be "
                        "published here before commercial launch.",
                    ],
                },
            ],
        },
    },
    "privacy": {
        "ru": {
            "title": "Политика конфиденциальности",
            "updated": "2026-09-12",
            "intro": (
                "Эта политика описывает, какие персональные данные обрабатывает сервис "
                "Такт, зачем и на каком основании — в соответствии с Федеральным законом "
                "«О персональных данных» № 152-ФЗ."
            ),
            "sections": [
                {
                    "title": "1. Оператор персональных данных",
                    "body": [
                        "Наименование, ОГРН/ИНН и юридический адрес оператора будут "
                        "указаны здесь после регистрации юридического лица или ИП и "
                        "юридической проверки документа. До этого момента считайте "
                        "документ черновиком.",
                    ],
                },
                {
                    "title": "2. Какие данные мы обрабатываем",
                    "body": [
                        "Имя и логин пользователя кабинета; контактные данные (телефон "
                        "и/или Telegram), указанные при заявке или регистрации; данные о "
                        "рабочих сменах, часах и ставках, которые вносит владелец или "
                        "управляющий; технические данные (IP-адрес, user-agent, время "
                        "запроса) — для защиты от злоупотреблений и расследования "
                        "инцидентов.",
                    ],
                },
                {
                    "title": "3. Основание и цели обработки",
                    "body": [
                        "Для владельца, управляющего и сотрудника — исполнение и "
                        "обеспечение работы договора на использование Сервиса. Для заявки "
                        "с публичной формы — согласие, которое вы даёте явным чекбоксом "
                        "при отправке.",
                        "Мы обрабатываем данные только для: предоставления функций "
                        "Сервиса (учёт смен и зарплаты), ответа на заявку, технической "
                        "поддержки и защиты от мошенничества и злоупотреблений.",
                    ],
                },
                {
                    "title": "4. Где хранятся данные",
                    "body": [
                        "Место размещения основной базы данных продакшена уточняется и "
                        "будет указано здесь до коммерческого запуска. Мы намерены "
                        "хранить персональные данные граждан РФ на территории РФ в "
                        "соответствии со ст. 18 152-ФЗ.",
                    ],
                },
                {
                    "title": "5. Передача третьим лицам",
                    "body": [
                        "Мы не продаём и не передаём данные рекламным или аналитическим "
                        "сервисам. Сообщения владельцу и сотрудникам доставляются через "
                        "Telegram Bot API — в этом объёме Telegram выступает технической "
                        "инфраструктурой доставки, а не самостоятельным получателем "
                        "данных для собственных целей.",
                    ],
                },
                {
                    "title": "6. Cookie и localStorage",
                    "body": [
                        "Подробности — в отдельном уведомлении об использовании cookie и "
                        "localStorage.",
                    ],
                },
                {
                    "title": "7. Срок хранения",
                    "body": [
                        "Данные хранятся, пока действует ваш аккаунт или пока это "
                        "необходимо для оказания услуги, после чего удаляются или "
                        "обезличиваются — кроме случаев, когда более долгий срок хранения "
                        "требует закон.",
                    ],
                },
                {
                    "title": "8. Ваши права",
                    "body": [
                        "Вы вправе запросить доступ к своим данным, их исправление, "
                        "удаление, а также отозвать согласие на обработку — в части, где "
                        "отзыв не противоречит уже заключённому договору. Обратитесь по "
                        "контакту ниже.",
                    ],
                },
                {
                    "title": "9. Контакты по вопросам данных",
                    "body": [
                        "Контакт для запросов, связанных с персональными данными, будет "
                        "указан здесь перед коммерческим запуском.",
                    ],
                },
            ],
        },
        "en": {
            "title": "Privacy Policy",
            "updated": "2026-09-12",
            "intro": (
                "This policy describes what personal data Takt processes, why, and on "
                "what basis — in line with Russian Federal Law No. 152-FZ “On "
                "Personal Data.”"
            ),
            "sections": [
                {
                    "title": "1. Data controller",
                    "body": [
                        "The legal entity name, registration number, and address of the "
                        "data controller will be published here once the entity is "
                        "registered and this document has had a legal review. Until then, "
                        "treat this document as a draft.",
                    ],
                },
                {
                    "title": "2. What data we process",
                    "body": [
                        "Workspace user name and login; contact details (phone and/or "
                        "Telegram) provided in a request or registration; shift, hours, "
                        "and rate data entered by the owner or a manager; technical data "
                        "(IP address, user agent, request time) for abuse prevention and "
                        "incident investigation.",
                    ],
                },
                {
                    "title": "3. Legal basis and purpose",
                    "body": [
                        "For the owner, manager, and employee — performance of the "
                        "service agreement. For a public-form request — the consent "
                        "you give via the explicit checkbox when submitting it.",
                        "We process data only to: provide Service features (shift and "
                        "payroll tracking), respond to your request, provide support, and "
                        "prevent fraud or abuse.",
                    ],
                },
                {
                    "title": "4. Where data is stored",
                    "body": [
                        "The production database's hosting location is still being "
                        "finalized and will be published here before commercial launch. "
                        "We intend to store Russian citizens' personal data within the "
                        "Russian Federation per Article 18 of 152-FZ.",
                    ],
                },
                {
                    "title": "5. Sharing with third parties",
                    "body": [
                        "We do not sell or share data with advertising or analytics "
                        "services. Notifications to owners and employees are delivered "
                        "via the Telegram Bot API — to that extent Telegram acts as "
                        "delivery infrastructure, not an independent recipient of data "
                        "for its own purposes.",
                    ],
                },
                {
                    "title": "6. Cookies and local storage",
                    "body": [
                        "See the separate cookie and local storage notice for details.",
                    ],
                },
                {
                    "title": "7. Retention",
                    "body": [
                        "Data is kept while your account is active or as long as needed "
                        "to provide the Service, then deleted or anonymized — unless "
                        "the law requires a longer retention period.",
                    ],
                },
                {
                    "title": "8. Your rights",
                    "body": [
                        "You may request access to your data, its correction or deletion, "
                        "or withdraw consent to processing — to the extent that "
                        "withdrawal does not conflict with an already-concluded "
                        "agreement. Reach out via the contact below.",
                    ],
                },
                {
                    "title": "9. Contact for data requests",
                    "body": [
                        "A contact for personal-data requests will be published here "
                        "before commercial launch.",
                    ],
                },
            ],
        },
    },
    "cookies": {
        "ru": {
            "title": "Уведомление о cookie и localStorage",
            "updated": "2026-09-12",
            "intro": (
                "Сервис не использует рекламные или аналитические cookie и не "
                "передаёт данные трекерам. Ниже — полный список того, что "
                "используется, и зачем."
            ),
            "sections": [
                {
                    "title": "1. Что мы используем",
                    "body": [
                        "session_token (cookie, httpOnly) — авторизованная сессия в "
                        "кабинете после входа.",
                        "CSRF-токен формы (cookie/сессия) — защита формы заявки и форм "
                        "кабинета от подделки запроса с чужого сайта.",
                        "site_lang (cookie) — запоминает выбранный язык интерфейса "
                        "(RU/EN).",
                        "Отметка о просмотре уведомления о cookie (localStorage) — "
                        "запоминает, что вы закрыли это уведомление, чтобы не показывать "
                        "его повторно.",
                    ],
                },
                {
                    "title": "2. Можно ли отключить выборочно",
                    "body": [
                        "Эти механизмы технически необходимы для работы кабинета и формы "
                        "заявки — например, без CSRF-токена форма не будет работать. "
                        "Отключить их по отдельности через настройки сайта нельзя. Вы "
                        "можете удалить cookie и localStorage в настройках браузера — "
                        "тогда потребуется войти заново, а уведомление появится снова.",
                    ],
                },
                {
                    "title": "3. Что мы не используем",
                    "body": [
                        "Рекламные сети, аналитику (Google Analytics, Яндекс.Метрику и "
                        "аналоги), пиксели социальных сетей и cookie сторонних сервисов "
                        "на публичном сайте.",
                    ],
                },
            ],
        },
        "en": {
            "title": "Cookie and Local Storage Notice",
            "updated": "2026-09-12",
            "intro": (
                "The Service does not use advertising or analytics cookies and does not "
                "share data with trackers. Below is the complete list of what is used, "
                "and why."
            ),
            "sections": [
                {
                    "title": "1. What we use",
                    "body": [
                        "session_token (cookie, httpOnly) — your signed-in workspace "
                        "session after login.",
                        "A form CSRF token (cookie/session) — protects the request "
                        "form and workspace forms from cross-site request forgery.",
                        "site_lang (cookie) — remembers your chosen interface "
                        "language (RU/EN).",
                        "A cookie-notice acknowledgement (localStorage) — remembers "
                        "that you dismissed this notice so it does not show again.",
                    ],
                },
                {
                    "title": "2. Can these be turned off individually",
                    "body": [
                        "These are technically required for the workspace and request "
                        "form to work — for example, the form will not submit "
                        "without a CSRF token. They cannot be toggled off individually "
                        "through a site setting. You can clear cookies and local storage "
                        "in your browser settings; you will then need to sign in again "
                        "and the notice will reappear.",
                    ],
                },
                {
                    "title": "3. What we do not use",
                    "body": [
                        "Advertising networks, analytics (Google Analytics, Yandex "
                        "Metrica, or similar), social-media pixels, or third-party "
                        "cookies on the public site.",
                    ],
                },
            ],
        },
    },
}
