/* --- КОНФИГУРАЦИЯ API --- */
const API_BASE_URL = 'https://oopsserver.onrender.com';
const API_INIT_AUTH_PATH = '/init-auth';


/* --- ФЛАГИ И ОБЩАЯ ЛОГИКА ИНИЦИАЛИЗАЦИИ --- */

window.pageLoaded = false;
window.dataLoaded = typeof productsData !== 'undefined';
let currentOrderToken = null; // Не используется, но оставлен


// --- ПЕРЕМЕННЫЕ ДЛЯ ДОСТАВКИ (СБРОС) ---
let DELIVERY_COST = 0; 
let DELIVERY_TERM = ''; 
let ITEMS_TOTAL_PRICE = 0; // Сумма только товаров
let CDEK_WIDGET_INSTANCE = null; // Не используется, но оставлен


// --- УТИЛИТЫ ---
const debounce = (func, delay) => {
    let timeoutId;
    return (...args) => {
        clearTimeout(timeoutId);
        timeoutId = setTimeout(() => {
            func.apply(null, args);
        }, delay);
    };
};

// Центральная функция для рендеринга продуктов на ГЛАВНОЙ странице
const renderHomePageProducts = () => {
    const productsContainer = document.getElementById('products-grid');
    
    const unavailableMessage = `
        <p style="
            display: block; 
            width: 100%;
            text-align: center; 
            grid-column: 1 / -1;
            margin: 40px 0; 
            padding: 20px; 
            font-weight: 500; 
            font-size: 1.2em;
            line-height: 1.5; 
            color: #888; 
            white-space: nowrap; 
        ">
            В данный момент нет доступных товаров.
        </p>
    `;
    
    if (!productsContainer) {
        return;
    }

    if (typeof productsData === 'undefined' || productsData.length === 0) {
        productsContainer.innerHTML = unavailableMessage;
        return;
    }
    
    const availableProducts = productsData.filter(product => product.isAvailable === true);
    
    if (availableProducts.length === 0) {
        productsContainer.innerHTML = unavailableMessage;
        return;
    }

    availableProducts.forEach(product => {
        
        let categorySlug = product.category;
        
        if (product.category === 'longsleeve' && product.slug === 'base-white') {
            categorySlug = 'long-sleeve'; 
        }
        
        const productUrl = `./categories/${categorySlug}/${product.slug}/index.html`;
        const imageUrl = `./images/${product.image}`; 
        const productCard = document.createElement('article');
        productCard.className = 'product-card';
        const formattedPrice = product.price.toLocaleString('ru-RU');

        productCard.innerHTML = 
            `<a href="${productUrl}" class="product-card__link"> 
                <div class="product-card__image-block" style="background-image: url('${imageUrl}');"></div>
                <div class="product-card__info">
                    <h3 class="product-card__name">${product.name}</h3>
                    <p class="product-card__price">${formattedPrice} ₽</p>
                </div>
            </a>
            
            <a href="${productUrl}" class="product-card__add-to-cart" data-no-copy style="text-decoration: none; display: flex; justify-content: center; align-items: center;">
                <i class="fas fa-info-circle"></i> 
                <span>Подробнее</span>
            </a>
        `;
        productsContainer.appendChild(productCard);
    });
};


// --- ЛОГИКА СТРАНИЦЫ ОФОРМЛЕНИЯ ПОСЛЕ TG (УПРОЩЕНА) ---
const checkCheckoutState = () => {
    const params = new URLSearchParams(window.location.search);
    const token = params.get('token');
    const tgId = params.get('tg_id');
    
    if (token || tgId) {
        history.replaceState(null, document.title, window.location.pathname);
        alert('Обратная ссылка на сайт не требуется. Для уточнения по заказу, пожалуйста, напишите в Telegram: @oopssupport');
    }
};

// --- ЛОГИКА API И ТОТАЛОВ ---

// 3. ОБЩАЯ ФУНКЦИЯ ДЛЯ ОБНОВЛЕНИЯ ОБЩЕЙ СУММЫ (Товары + Доставка)
const updateCartTotal = () => {
    const grandTotal = ITEMS_TOTAL_PRICE; 
    
    const itemsPriceEl = document.getElementById('items-total-price');
    const grandTotalEl = document.getElementById('cart-grand-total-price');
    
    // Скрываем блок доставки
    const deliveryPriceDisplayFooter = document.getElementById('delivery-price-display-footer');
    const deliveryPriceContainer = deliveryPriceDisplayFooter ? deliveryPriceDisplayFooter.closest('div') : null;
    
    if(deliveryPriceContainer) deliveryPriceContainer.style.display = 'none';
    if (deliveryPriceDisplayFooter) deliveryPriceDisplayFooter.textContent = '0 ₽';

    
    if (itemsPriceEl) {
        itemsPriceEl.textContent = ITEMS_TOTAL_PRICE.toLocaleString('ru-RU') + ' ₽';
    }
    
    const oldTotalEl = document.getElementById('cart-total-price');
    
    // Обновляем главный итоговый элемент
    if (grandTotalEl) {
        grandTotalEl.textContent = grandTotal.toLocaleString('ru-RU') + ' ₽';
    } else if (oldTotalEl) {
        // Если используется старый ID, обновляем его
        oldTotalEl.textContent = grandTotal.toLocaleString('ru-RU') + ' ₽';
    }
};


// 1. ИНИЦИАЦИЯ ЗАКАЗА (КЛЮЧЕВАЯ ФУНКЦИЯ)
const initOrderProcess = async () => {
    // Получаем корзину (убеждаемся, что ключ 'oopsCart' верный)
    const cart = JSON.parse(localStorage.getItem('oopsCart')) || []; 
    const cartActionBtn = document.getElementById('cart-action-btn');
    
    // Вспомогательная функция для закрытия оверлея
    const closeCart = () => {
        const cartOverlay = document.getElementById('cart-overlay');
        if (cartOverlay) cartOverlay.classList.remove('open');
        document.body.style.overflow = '';
    };

    if (cart.length === 0) {
        alert('Корзина пуста.');
        return;
    }
    
    if (cartActionBtn) {
        cartActionBtn.textContent = 'Подготовка заказа...';
        cartActionBtn.disabled = true;
    }

    // Собираем данные для бэкенда
    const cartData = {
        items: cart.map(item => ({
            id: item.id,
            name: item.name,
            price: item.price,
            size: item.size,
            quantity: item.quantity
        })),
        total_amount: cart.reduce((sum, i) => sum + i.price * i.quantity, 0)
    };
    
    try {
        const response = await fetch(API_BASE_URL + API_INIT_AUTH_PATH, {
            method: 'POST',
            headers: { 
                'Content-Type': 'application/json' 
            },
            body: JSON.stringify(cartData)
        });
        
        // --- ПРОВЕРКА ОТВЕТА СЕРВЕРА ---
        if (response.ok) {
            const result = await response.json();
            
            if (result.telegram_bot_url) { // Ожидаем прямую ссылку на бота
                // Успешно, очищаем корзину и перенаправляем
                localStorage.setItem('oopsCart', JSON.stringify([]));
                window.updateCartUI(); 
                closeCart();
                
                // alert('Заказ создан! Переходим в Telegram-бот для оформления (ФИО, адрес, подтверждение номера, оплата). Для уточнения: @oopssupport');
                window.location.href = result.telegram_bot_url; 
                
                // !!! ИСПРАВЛЕНИЕ !!!: Немедленно завершаем функцию, чтобы избежать сброса кнопки.
                return; 
            } else {
                 alert('Заказ создан! Но не удалось получить ссылку на бота. Пожалуйста, перейдите в Telegram и начните диалог с ботом, чтобы продолжить оформление: @oopssupport');
            }
        } else {
            // Ошибка 4xx/5xx от Render-сервера. Пытаемся получить текст ошибки.
            const errorText = await response.text();
            console.error("Ошибка API:", response.status, errorText);
            alert(`Ошибка сервера при инициации заказа. Код: ${response.status}. Проверьте логи Render.`);
        }
    } catch (e) {
        // Ошибка сети или CORS
        console.error("Ошибка сети при инициации заказа:", e);
        alert('Ошибка сети. Проверьте адрес API и настройки CORS на Render.');
    } finally {
        // Этот блок выполняется, только если не было успешного return выше
        if (cartActionBtn) {
            cartActionBtn.textContent = 'Оформить заказ в Telegram';
            cartActionBtn.disabled = false;
        }
    }
};


/* --- ЛОГИКА UI КОРЗИНЫ (ИЗМЕНЕНА ДЛЯ КОНСИСТЕНТНОСТИ) --- */

document.addEventListener('DOMContentLoaded', () => {

    // --- 1. Меню (UI) ---
    const sideMenu = document.getElementById('menu');
    const menuToggleBtn = document.querySelector('.header__burger-btn');
    const menuCloseBtn = document.querySelector('.side-menu__close-btn');
    
    const openMenu = () => {
        if (sideMenu) {
            sideMenu.classList.add('is-active');
            document.body.classList.add('menu-open');
            window.location.hash = '#open-menu';
        }
    };

    const closeMenu = () => {
        if (sideMenu) {
            sideMenu.classList.remove('is-active');
            document.body.classList.remove('menu-open');
            if (window.location.hash === '#open-menu') {
                history.replaceState('', document.title, window.location.pathname + window.location.search);
            }
        }
    };
    
    if (menuToggleBtn) menuToggleBtn.addEventListener('click', openMenu);
    if (menuCloseBtn) menuCloseBtn.addEventListener('click', closeMenu);

    document.querySelectorAll('.side-menu__nav a').forEach(link => {
        link.addEventListener('click', closeMenu);
    });

    if (window.location.hash === '#open-menu') {
        openMenu();
    }
    
    document.querySelectorAll('.menu-item__toggle').forEach(button => {
        button.addEventListener('click', () => {
            
            const submenu = button.nextElementSibling;
            if (!submenu || !submenu.classList.contains('submenu')) return;

            const icon = button.querySelector('.submenu-icon');

            document.querySelectorAll('.submenu').forEach(otherSubmenu => {
                if (otherSubmenu !== submenu && otherSubmenu.classList.contains('submenu--open')) {
                    otherSubmenu.classList.remove('submenu--open');
                    const otherButton = otherSubmenu.previousElementSibling;
                    if (otherButton) {
                        const otherIcon = otherButton.querySelector('.submenu-icon');
                        if (otherIcon) {
                            otherIcon.style.transform = 'rotate(0deg)';
                        }
                    }
                }
            });

            submenu.classList.toggle('submenu--open');
            if (icon) {
                icon.style.transform = submenu.classList.contains('submenu--open') ? 'rotate(180deg)' : 'rotate(0deg)';
            }
        });
    });


    // --- 2. Слайдер (Без изменений) ---
    const sliderContainer = document.querySelector('.slider-container');
    const slides = document.querySelectorAll('.slide');
    const indicatorItems = document.querySelectorAll('.indicator-item');
    const prevBtn = document.querySelector('.prev-btn');
    const nextBtn = document.querySelector('.next-btn');
    const slideDuration = 4000;
    let currentSlide = 0;
    let intervalId;
    let timeoutId;
    let touchstartX = 0;
    let touchendX = 0;
    const swipeThreshold = 50;

    function showSlide(index) {
        if (index >= slides.length) {
            index = 0;
        } else if (index < 0) {
            index = slides.length - 1;
        }
        
        slides.forEach(slide => slide.classList.remove('active'));
        indicatorItems.forEach(item => item.classList.remove('active'));
        
        slides[index].classList.add('active');
        indicatorItems[index].classList.add('active');
        currentSlide = index;
        
        startIndicatorAnimation();
    }

    function startIndicatorAnimation() {
        indicatorItems.forEach(item => {
            const progress = item.querySelector('.indicator-progress');
            progress.style.transition = 'none';
            progress.style.width = '0%';
        });

        const activeProgress = indicatorItems[currentSlide].querySelector('.indicator-progress');

        clearTimeout(timeoutId);
        timeoutId = setTimeout(() => {
            activeProgress.style.transition = `width ${slideDuration / 1000}s linear`;
            activeProgress.style.width = '100%';
        }, 50); 
    }

    function nextSlide() {
        showSlide(currentSlide + 1);
        restartAutoSlide();
    }
    
    function prevSlide() {
        showSlide(currentSlide - 1);
        restartAutoSlide();
    }

    function restartAutoSlide() {
        clearInterval(intervalId);
        intervalId = setInterval(nextSlide, slideDuration);
    }
    
    if (slides.length > 0) {
        showSlide(0); 
        restartAutoSlide();

        if (prevBtn) prevBtn.addEventListener('click', prevSlide);
        if (nextBtn) nextBtn.addEventListener('click', nextSlide);

        if (sliderContainer) {
             sliderContainer.addEventListener('touchstart', e => {
                touchstartX = e.changedTouches[0].screenX;
            }, false);

            sliderContainer.addEventListener('touchend', e => {
                touchendX = e.changedTouches[0].screenX;
                handleGesture();
            }, false);
        }

        function handleGesture() {
            if (touchendX < touchstartX - swipeThreshold) {
                nextSlide();
            }
            if (touchendX > touchstartX + swipeThreshold) {
                prevSlide();
            }
        }
    }


    // --- 3. Корзина (Cart) ---
    
    let cart = JSON.parse(localStorage.getItem('oopsCart')) || []; 
    const cartOverlay = document.getElementById('cart-overlay');
    const cartCloseBtn = document.getElementById('cart-close');
    const cartList = document.getElementById('cart-list');
    const cartActionBtn = document.getElementById('cart-action-btn');
    const cartTotalBlock = document.getElementById('cart-total-block');
    
    const cartItemsView = document.getElementById('cart-items-view');
    const checkoutView = document.getElementById('checkout-view'); 
    const backToCartBtn = document.getElementById('back-to-cart'); 
    
    const headerCartBtn = document.querySelector('.header__cart'); 
    
    // Создание иконки количества товара
    const badge = document.createElement('div');
    badge.className = 'cart-badge';
    badge.style.display = 'none';
    
    if (headerCartBtn) { 
        headerCartBtn.appendChild(badge);
    }
    

    const saveCart = () => {
        localStorage.setItem('oopsCart', JSON.stringify(cart));
        updateCartUI();
    };

    const updateCartUI = () => {
        const totalQty = cart.reduce((sum, item) => sum + item.quantity, 0);
        if (totalQty > 0) {
            badge.textContent = totalQty;
            badge.style.display = 'flex';
        } else {
            badge.style.display = 'none';
        }

        if (cart.length === 0) {
            DELIVERY_COST = 0;
            DELIVERY_TERM = '';
            ITEMS_TOTAL_PRICE = 0;
            
            if (document.getElementById('calculated-delivery-info')) {
                document.getElementById('calculated-delivery-info').style.display = 'none';
            }
            if (document.getElementById('delivery-status-block')) {
                document.getElementById('delivery-status-block').innerHTML = '';
            }
            if (checkoutView) {
                checkoutView.classList.remove('active');
                checkoutView.style.display = 'none'; 
            }
            
            if (cartList) cartList.innerHTML = '<div class="cart-empty-msg">Ваша корзина пуста</div>';
            if (cartActionBtn) cartActionBtn.disabled = true;
            updateCartTotal(); 
            return;
        }

        if (cartActionBtn) cartActionBtn.disabled = false;
        let totalPrice = 0;
        
        const listHtml = cart.map((item, index) => {
            totalPrice += item.price * item.quantity;
            
            const imageUrl = item.image || ''; 
            const productLink = item.productUrl || '#'; 

            return `
                <div class="cart-item">
                    <div class="cart-item__img" style="background-image: url('${imageUrl}');"></div>
                    <div class="cart-item__details">
                        <div>
                            <a href="${productLink}" style="text-decoration: none;"><div class="cart-item__name">${item.name}</div></a>
                            <div class="cart-item__variant">Размер: ${item.size}</div>
                        </div>
                        <div class="cart-item__controls">
                            <div class="qty-selector-small">
                                <button class="qty-btn-small" onclick="window.changeCartQty(${index}, -1)">-</button>
                                <div class="qty-val-small">${item.quantity}</div>
                                <button class="qty-btn-small" onclick="window.changeCartQty(${index}, 1)">+</button>
                            </div>
                            <div class="cart-item__price">${(item.price * item.quantity).toLocaleString('ru-RU')} ₽</div>
                        </div>
                         <div class="cart-item__remove" onclick="window.removeCartItem(${index})">Удалить</div>
                    </div>
                </div>
            `;
        }).join('');
        
        ITEMS_TOTAL_PRICE = totalPrice; 

        if (cartList) cartList.innerHTML = listHtml;
        
        updateCartTotal(); 
    };
    
    // Делаем updateCartUI глобальной
    window.updateCartUI = updateCartUI;

    window.changeCartQty = (index, change) => {
        if (cart[index] && cart[index].quantity + change > 0) {
            cart[index].quantity += change;
        } else if (cart[index]) {
            if(confirm('Удалить товар из корзины?')) {
                cart.splice(index, 1);
            }
        }
        saveCart();
    };

    window.removeCartItem = (index) => {
        cart.splice(index, 1);
        saveCart();
    };
    
    window.addToCartGlobal = (productObj) => {
        const existing = cart.find(i => i.id === productObj.id && i.size === productObj.size);
        if (existing) {
            existing.quantity += productObj.quantity;
        } else {
            cart.push(productObj);
        }
        saveCart();
        openCart();
    };

    const openCart = () => {
        if (cartOverlay) {
             cartOverlay.classList.add('open');
             document.body.style.overflow = 'hidden';
             showItemsView();
        }
    };
    
    const closeCart = () => {
        if (cartOverlay) {
            cartOverlay.classList.remove('open');
            document.body.style.overflow = '';
        }
    };

    const showItemsView = () => {
        if (cartItemsView) cartItemsView.style.display = 'block';
        if (checkoutView) checkoutView.classList.remove('active'); 
        if (checkoutView) checkoutView.style.display = 'none';
        
        if (cartTotalBlock) cartTotalBlock.style.visibility = 'visible';
        
        if (cartActionBtn) {
            cartActionBtn.textContent = 'Оформить заказ в Telegram'; 
            cartActionBtn.onclick = initOrderProcess; // КЛЮЧЕВАЯ ПРИВЯЗКА
        }
        
        if (backToCartBtn) backToCartBtn.style.display = 'none'; 
        const cartHeader = document.querySelector('.cart-header h2');
        if (cartHeader) cartHeader.textContent = 'Корзина';
    };

    // Присвоение обработчиков событий
    if (headerCartBtn) { 
        headerCartBtn.addEventListener('click', (e) => {
            e.preventDefault();
            openCart();
        });
    }
    
    if (cartCloseBtn) cartCloseBtn.addEventListener('click', closeCart);
    
    if (cartOverlay) {
        cartOverlay.addEventListener('click', (e) => {
            if (e.target === cartOverlay) closeCart();
        });
    }

    if (backToCartBtn) backToCartBtn.addEventListener('click', showItemsView);

    // --- 4. Привязка к API: КЛЮЧЕВАЯ ПРИВЯЗКА В КОНЦЕ ---
    if (cartActionBtn) { 
        cartActionBtn.onclick = initOrderProcess; 
    }
    
    // --- 5. Запуск инициализации ---
    window.pageLoaded = true;
    
    if (window.dataLoaded) {
        window.initPage();
    }
});


// Главная функция инициализации, вызывается при полной готовности
window.initPage = () => {
    
    if (window.initPageLogic) { 
        window.initPageLogic();
    }
    
    const productsGrid = document.getElementById('products-grid');
    if (productsGrid) {
        renderHomePageProducts();
    }
    
    // Обновление корзины
    if (window.updateCartUI) {
        window.updateCartUI();
    }
    
    checkCheckoutState();
};
