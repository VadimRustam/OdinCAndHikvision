__version__ = "1.0.0"
__author__ = "Вадим Рустамович"

"""Модуль для добавления сотрудников на терминал Hikvision карта + лицо.
Выдача 1 доступа тем людям у кого '"by": true'.
При добавлении данных но сотрудник уже присутсвует, на терминале. Он обновляется.

Алгоритм работы такой:
Вводится ИИН и ФИО
Удаляются у пользователя все карты (потому что, если есть лицо, то оно обновляется. Но если есть карта добавляется
новая, и карта будет занята, и на 1 человека 2 карты, хотя должна быть 1, так быть не должно)
С терминала взбирается карта, и возвращается ее номер. В строковом формате. Так же логируется ошибка.
После добавляется человек add_user
добавляется ему карта
Берется у него лицо с терминала. Если лицо не приложили будет ошибка.
После лицо скачивается, где имя это его ИИН (для механики одной, после укажу в документации)
И уже после лицо добавляется к сотруднику
Так же можно добавлять, человека по фото
"""

import logging
from pathlib import Path
import random
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional, cast
import requests
from requests.auth import HTTPDigestAuth
import json
import sys
import os

logger = logging.getLogger("HikvisionClient")
logger.setLevel(logging.DEBUG)

class HikvisionRequestFailed(Exception):
    """Общая ошибка запроса после всех попыток"""
    def __init__(
        self,
        url: str,
        message: str,
        last_error: Optional[str] = None,
        last_response: Optional[str] = None
    ) -> None:
        self.url = url
        self.last_error = last_error
        self.last_response = last_response
        super().__init__(f"{message}\nURL: {url}\nПоследняя ошибка: {last_error}\nОтвет: {last_response}")

class CardAlreadyAssignedError(Exception):
    """Исключение при попытке добавить карту, которая уже принадлежит другому пользователю."""
    def __init__(self, card_no: str, message: str = "Карта уже привязана к другому пользователю"):
        self.card_no = card_no
        super().__init__(f"{message}. Карта: {card_no}")

class CardCaptureTimeoutError(Exception):
    """
    Карта не была приложена к терминалу в течение отведенного времени.
    Terминал в этом случае отвечает HTTP 404 + statusCode == 3 ("Device Error", subStatusCode "deviceError").
    """
    def __init__(self, message: str = "Карта не была приложена к терминалу в отведенное время. Попробуйте еще раз."):
        super().__init__(message)

class FaceCaptureTimeoutError(Exception):
    """
    Лицо не было распознано терминалом в течение отведенного времени.
    Терминал в этом случае отвечает HTTP 400 + statusCode == 3 ("Device Error", subStatusCode "captureTimeout").
    """
    def __init__(self, message: str = "Лицо не было распознано терминалом в отведенное время. Попробуйте еще раз."):
        super().__init__(message)

class HikvisionError(Exception):
    """Не введены, либо не правильно заполненны логин/пароль от hikvision"""
    pass

class HikvisionClient:
    """Базовый клиент для работы с ISAPI Hikvision."""

    def __init__(self, ip_address: str = "", username: str = "", password: str = "") -> None:
        if not all(
            [
                ip_address and ip_address.strip(),
                username and username.strip(),
                password and password.strip(),
            ]
        ):
            # logger.critical("Инициализация провалена: отсутствуют учетные данные или не правильно были введены.")
            raise HikvisionError("ip_address, username, password обязательны")

        self.ip_address: str = ip_address.strip()
        self.username: str = username.strip()
        self.password: str = password.strip()

        self.base_url: str = f"http://{self.ip_address}/ISAPI"
        self.headers_json: Dict[str, str] = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        self.headers_xml: Dict[str, str] = {
            "Content-Type": "application/xml",
            "Accept": "application/json",
        }

        self.auth: HTTPDigestAuth = HTTPDigestAuth(self.username, self.password)

        # Постоянная сессия вместо requests.request(): переиспользует TCP-соединение
        # (keep-alive), а не открывает новое на каждый запрос. Терминалы Hikvision
        # обычно ограничивают число одновременных соединений, поэтому это заодно
        # снижает риск повторяющегося 401 из-за упора в этот лимит.
        self.session: requests.Session = requests.Session()
        self.session.auth = self.auth

    def reset_auth(self) -> None:
        """Сброс и повторная инициализация Digest авторизации."""
        logger.debug("Обновление/сброс объекта HTTPDigestAuth.")
        self.auth = HTTPDigestAuth(self.username, self.password)
        self.session.auth = self.auth

    def request(
        self, method: str, url: str, retries: int = 3, **kwargs: Any
    ) -> requests.Response:
        """Централизованный метод для выполнения HTTP-запросов с обработкой 401, 5xx и повторами."""
        
        last_error = None
        last_response = None

        for attempt in range(1, retries + 1):
            try:
                logger.debug(
                    f"Попытка {attempt}/{retries}: {method} {url}\n"
                )
                response = self.session.request(
                    method=method, url=url, timeout=30, **kwargs
                )
                
                if 200 <= response.status_code < 300:
                    return response

                # Обработка истекшей авторизации (401)
                if response.status_code == 401:
                    logger.warning(
                        f"Получен статус 401 на попытке {attempt}. Сбрасываем авторизацию... Текст ответа: {response.text} \n"
                    )

                    if attempt == retries:
                        break
                    
                    self.reset_auth()
                    continue
                
                if 400 <= response.status_code < 500:
                    raise HikvisionRequestFailed(
                        url=url,
                        message=f"Client error (HTTP {response.status_code})",
                        last_error=str(response.status_code),
                        last_response=response.text
                    )

                # Обработка серверных ошибок (5xx)
                if 500 <= response.status_code < 600:
                    last_response = response.text
                    last_error = str(response.status_code)
                    logger.warning(
                        f"Сервер вернул ошибку {response.status_code} на попытке {attempt}. Ожидание... Текст ответа: {response.text} \n"
                    )

                    if attempt == retries:
                        break

                    continue
                
                last_response = response.text
                last_error = str(response.status_code)
                logger.error(
                    f"attempt {attempt}/{retries} завершился с ошибкой {url}: response text {last_response}, status code {last_error}"
                )
                break

            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                logger.error(f"Таймаут соединения при запросе {url}: {e}")
                last_error = str(e)

                if attempt == retries:
                    break

                continue
                
        raise HikvisionRequestFailed(url, "Не удалось выполнить запрос после нескольких попыток.", last_error, last_response)


class AddUserByAutoCard:
    """Класс для автоматизации добавления пользователей, карт и лиц через терминал."""

    def __init__(self, connection: HikvisionClient) -> None:
        self.api = connection
        self._ns: Dict[str, str] = {
            "ns": "http://www.isapi.org/ver20/XMLSchema",
        }

    def grant_daily_access_to_all(self, user_ids: List[str]) -> None:
        """Выдать каждому пользователю 1 доступ."""
        try:
            for user_id in user_ids:
                self.set_max_visits(user_id)
            logger.info("Суточный доступ успешно обновлен для всех пользователей.")
        except Exception as e:
            logger.error(f"Ошибка при выдаче суточного доступа: {e}")

    def set_max_visits(self, employee_no: str) -> None:
        """Установка ограничений на открытие дверей (1 раз в сутки)."""
        url = f"{self.api.base_url}/AccessControl/UserInfo/Modify?format=json"
        payload: Dict[str, Dict[str, Any]] = {
            "UserInfo": {
                "employeeNo": str(employee_no),
                "maxOpenDoorTime": 1,
                "openDoorTime": 0,
            }
        }
        self.api.request("PUT", url, headers=self.api.headers_json, json=payload)
    
    def add_photo_to_user_by_url(self, face_url: str, employee_no: str) -> str:
        """Привязка фотографии к пользователю по URL."""
        if not face_url:
            raise ValueError("Передан пустой face_url")

        url = f"{self.api.base_url}/Intelligent/FDLib/FDSetUp?format=json"
        payload: Dict[str, Any] = {
            "faceLibType": "blackFD",
            "FDID": "1",
            "FPID": str(employee_no),
            "bornTime": "1990-01-01",
            "saveFacePic": True,
            "faceURL": face_url,
        }

        response = self.api.request("PUT", url, headers=self.api.headers_json, json=payload)
        return response.text

    def add_card(self, card_number: str, employee_no: str) -> str:
        """Привязка карты к ID пользователя."""
        if not card_number:
            raise ValueError("Передан пустой card_number")

        request_url = f"{self.api.base_url}/AccessControl/CardInfo/SetUp?format=json"
        data = {
            "CardInfo": {
                "employeeNo": str(employee_no),
                "cardNo": str(card_number),
                "cardType": "normalCard",
                "leaderCard": "1",
            }
        }

        try:
            response = self.api.request("PUT", request_url, headers=self.api.headers_json, json=data)
        except HikvisionRequestFailed as e:
            logger.error(f"Карта уже добавлена: {e}")
            if self.extract_hikvision_status_code(e.last_response) == 6:
                logger.warning("Карта уже добавлена.")
                raise CardAlreadyAssignedError(card_number) from e
            raise
        return response.text

    def extract_hikvision_status_code(self, response: Optional[str]) -> Optional[int]:
        """
        Извлекает значение statusCode из ответа устройства Hikvision.

        Устройство может вернуть ошибку как в формате JSON, например:
            {"statusCode": 3, "statusString": "Device Error", "subStatusCode": "deviceError", ...}
        так и в формате XML, например:
            <ResponseStatus ...><statusCode>3</statusCode><subStatusCode>captureTimeout</subStatusCode>...</ResponseStatus>

        statusCode == 3 ("Device Error") в обоих случаях означает, что устройство
        не получило ожидаемое действие пользователя: карта не была приложена,
        либо лицо не было распознано в течение отведенного времени.

        Возвращает None, если ответ пустой, не распознан, либо statusCode не найден.
        """
        if not response:
            return None

        try:
            data = json.loads(response)
            if isinstance(data, dict):
                typed_data = cast(Dict[str, Any], data)
                status_code = typed_data.get("statusCode")
                if isinstance(status_code, (int, str)):
                    return int(status_code)
        except (json.JSONDecodeError, ValueError, TypeError):
            pass

        try:
            ns = {
            'ns': "http://www.hikvision.com/ver10/XMLSchema",
            }
            root = ET.fromstring(response)
            status = root.find("ns:statusCode", ns)
            if status is not None and status.text is not None: # Должно быть status.text.strip() != "", как будто так лучше
                return int(status.text.strip())
        except (ET.ParseError, ValueError):
            pass

        
    def get_card_no_by_terminal(self) -> str:
        """Перевод терминала в режим чтения карты и получение её номера."""
        request_url = f"{self.api.base_url}/AccessControl/CaptureCardInfo?format=json"
        logger.info("Команда отправлена. Пожалуйста, приложите карту к терминалу...")
        
        try:
            response = self.api.request("GET", request_url, headers=self.api.headers_json)
        except HikvisionRequestFailed as e:
            """
            Терминал отвечает HTTP 404 + statusCode == 3 ("Device Error", subStatusCode "deviceError"),
            если карта не была приложена в течение отведенного времени.
            Это отдельная, "ожидаемая" ошибка - сообщаем о ней отдельным исключением,
            чтобы выше (например, в Odoo) можно было показать понятное сообщение пользователю.
            """
            if self.extract_hikvision_status_code(e.last_response) == 3:
                logger.warning("Карта не была приложена к терминалу в течение отведенного времени.")
                raise CardCaptureTimeoutError() from e
            raise

        try:
            data = response.json()
            card_no = data.get("CardInfo", {}).get("cardNo")
            if not card_no:
                raise HikvisionRequestFailed(
                    url=request_url,
                    message="В ответе устройства отсутствует номер карты (CardInfo.cardNo).",
                    last_error="Missing cardNo",
                    last_response=response.text
                )
            
            logger.info(f"Карта успешно считана. Номер: {card_no}")
            return str(card_no)
        except (json.JSONDecodeError, KeyError) as e:
            logger.error(f"Ошибка обработки JSON при чтении карты: {e}. Ответ: {response.text}")
            raise HikvisionRequestFailed(
                url=request_url,
                message="Не удалось прочитать/распарсить данные карты.",
                last_error=f"JSONDecodeError: {str(e)}",
                last_response=response.text
            ) from e

    def get_face_terminal(self) -> str:
        """Запуск команды захвата лица на терминале и получение URL фото."""
        url = f"{self.api.base_url}/AccessControl/CaptureFaceData"
        xml_payload = """<CaptureFaceDataCond xmlns="http://www.isapi.org/ver20/XMLSchema" version="2.0">
                         </CaptureFaceDataCond>"""

        logger.info("Команда отправлена. Пожалуйста, посмотрите в терминал для сканирования лица...")

        try:
            response = self.api.request("POST", url, headers=self.api.headers_xml, data=xml_payload)
        except HikvisionRequestFailed as e:
            """
            Терминал отвечает HTTP 400 + statusCode == 3 ("Device Error", subStatusCode "captureTimeout"),
            если лицо не было распознано в течение отведенного времени.
            Это отдельная, "ожидаемая" ошибка - сообщаем о ней отдельным исключением,
            чтобы выше (например, в Odoo) можно было показать понятное сообщение пользователю.
            """
            if self.extract_hikvision_status_code(e.last_response) == 3:
                logger.warning("Лицо не было распознано терминалом в течение отведенного времени.")
                raise FaceCaptureTimeoutError() from e
            raise

        try:
            root = ET.fromstring(response.text)
            face_url_node = root.find("ns:faceDataUrl", self._ns)

            if face_url_node is None or not face_url_node.text:
                raise HikvisionRequestFailed(
                    url=url,
                    message="Терминал не вернул URL лица. Возможно, лицо не распознано.",
                    last_error="Missing faceDataUrl",
                    last_response=response.text
                )

            logger.info(f"Лицо успешно захвачено. URL: {face_url_node.text}")
            return str(face_url_node.text)
        except ET.ParseError as e:
            logger.error(f"Не удалось распарсить XML ответ от терминала: {e}")
            raise HikvisionRequestFailed(
                url=url,
                message="Ошибка парсинга XML при захвате лица.",
                last_error=f"ET.ParseError: {str(e)}",
                last_response=response.text
            ) from e
        
    def add_user(self, id_people: str, username: str) -> str:
        """Добавление учетной записи человека (без фото и карты)."""
        request_url = f"{self.api.base_url}/AccessControl/UserInfo/SetUp?format=json"
        payload: Dict[str, Dict[str, Any]] = {
            "UserInfo": {
                "employeeNo": str(id_people),
                "name": username,
                "userType": "visitor", # "gender" так же можно указать такое поле 
                "Valid": {
                    "enable": True,
                    "beginTime": "2000-01-01T00:00:00",
                    "endTime": "2037-12-31T23:59:59",
                    "timeType": "local",
                },
                "doorRight": "1",
                "RightPlan": [{"doorNo": 1, "planTemplateNo": "1"}],
                "maxOpenDoorTime": 1,
                "userVerifyMode": "faceAndCard", # card
            }
        }
        response = self.api.request("PUT", request_url, headers=self.api.headers_json, json=payload)
        return response.text

    # метод для теста, будет вводится с интфейрса в odoo
    def generate_id(self) -> str:
        """Генерация уникального 7-значного ID."""
        return str(random.randint(1000000, 9999999))

    def delete_card(self, card_number: str, employee_no: str) -> None:
        request_url = f"{self.api.base_url}/AccessControl/CardInfo/Delete?format=json"
        data = {
            "CardInfoDelCond": {
                "employeeNo": employee_no,
                "cardNo": card_number, # "cardType": "normalCard", "leaderCard": "1" параметры какие я использовал, пусть будут
            }
        }
        self.api.request("PUT", request_url, headers=self.api.headers_json, json=data)
    
    def delete_all_cards_for_user(self, user_id: str, cards: Optional[List[str]]) -> None:
        if cards is None or cards == []:
            logger.info(f"У пользователя с ИИН {user_id} нет карт для удаления.")
            return
        logger.info(f"У пользователя с ИИН {user_id} карта {cards} ---")
        for card in cards:
            self.delete_card(card, user_id)
            
    # Оптимизированный поиск карт конкретного пользователя
    def get_all_user_cards(self, user_id: str) -> Optional[List[str]]:
        request_url = f'{self.api.base_url}/AccessControl/CardInfo/Search?format=json'
        data: Dict[str, Dict[str, Any]] = {
            "CardInfoSearchCond": {
                "searchID": "1",
                "maxResults": 10,
                "searchResultPosition": 0,
                "EmployeeNoList": [{"employeeNo": str(user_id)}]
            }
        }  
        response = self.api.request("POST", request_url, headers=self.api.headers_json, json=data)
        cards = response.json().get("CardInfoSearch", {}).get("CardInfo", [])
        return [c.get("cardNo") for c in cards if c.get("cardNo")]
    
    def download_face_url(self, url: str, user_id: str) -> str:
        """Скачивает фото лица по URL и сохраняет локально. Возвращает путь к сохранённому файлу."""
        print(f"Downloading face image for user: {user_id} url {url}")
        img_response = self.api.request("GET", url, headers=self.api.headers_json)

        file_path = os.path.join(r"C:\Users\Вадим\Desktop\Hickvision&Odoo\prod\photo", f"{user_id}.jpg")
        with open(file_path, "wb") as f:
            f.write(img_response.content)

        return file_path
    
    def add_photo_by_people_photo(self, employee_no: str, photo_path: str) -> str:
        """Загрузка фото лица напрямую файлом (multipart), в отличие от add_photo_to_user_by_url (по URL)."""
        url = f'{self.api.base_url}/Intelligent/FDLib/FaceDataRecord?format=json'
        payload: Dict[str, Any] = {
            "faceLibType": "blackFD",
            "FDID": "1",
            "FPID": employee_no,
            "bornTime": "1990-01-01",
            "saveFacePic": True,
        }

        files: Dict[str, Any] = {
            "faceURL": (None, json.dumps(payload), "application/json"),
            "img":     ("facePic.jpg", Path(photo_path).read_bytes(), "image/jpeg"),
        }

        # NOTE: data=payload и files["faceURL"] содержат одни и те же поля дважды —
        # один раз как обычные multipart-поля, второй раз как JSON внутри "faceURL".
        # Проверь по документации ISAPI, что терминал ожидает оба варианта сразу,
        # а не только один из них.
        response = self.api.request(
            "POST",
            url,
            data=payload,
            files=files,
        )

        return response.text

if __name__ == "__main__":
    # Настройки для проверки

    try:

        ip_address = os.getenv("HIKVISION_IP")
        username = os.getenv("HIKVISION_USERNAME")
        password = os.getenv("HIKVISION_PASSWORD")

        if not ip_address or not username or not password:
            raise HikvisionError("ip_address, username, password обязательны")

        connection = HikvisionClient(
            ip_address=ip_address,
            username=username, 
            password=password
        )
    except HikvisionError:
        logger.critical("Инициализация провалена: отсутствуют учетные данные или не правильно были введены.", exc_info=True)
        # вывод в Odoo odoo.User("Данные не корректно введены")
        sys.exit(1)

    client = AddUserByAutoCard(
        connection=connection
    )

    try:
        client.grant_daily_access_to_all([])
        username = "ФИО"
        user_id = "1" # метод для теста, будет ИИН человека вводится, уникальный идентификатор
        
        logger.info(f"--- НАЧАЛО ПРОЦЕССА: Создание пользователя {username} с ID {user_id} ---")

        card_ids = client.get_all_user_cards(user_id)
        client.delete_all_cards_for_user(user_id, card_ids)

        # 1. Считываем карту
        try:
            card_id = client.get_card_no_by_terminal()
            logger.debug(f"Ответ get_face_terminal: {card_id}")
        except CardCaptureTimeoutError as e:
            logger.warning(f"{e}")
            # вывод в Odoo: odoo.User("Карта не была приложена к терминалу. Попробуйте еще раз.")
            sys.exit(1)
        
        # 3. Добавляем пользователя
        res_user = client.add_user(user_id, username)
        logger.debug(f"Ответ add_user: {res_user}")

        # 4. Добавляем карту
        try:
            res_card = client.add_card(card_id, user_id)
            logger.debug(f"Ответ add_card: {res_card}")
        except CardAlreadyAssignedError as e:
            logger.warning(f"Карта с номером {card_id} уже назначена другому пользователю: {e}")
            # вывод в Odoo odoo.User("Карта уже занята человеком")
            sys.exit(1)
                
        try:
            face_url = client.get_face_terminal()
            logger.debug(f"Ответ get_face_terminal: {face_url}")
        except FaceCaptureTimeoutError as e:    
            logger.warning(f"{e}")
            # вывод в Odoo: odoo.User("Лицо не было распознано терминалом. Попробуйте еще раз.")
            sys.exit(1)
        
        # # # метод self.api.request автоматически сделает reset_auth(),
        # # # если сессия здесь упадет по 401 ошибке из-за долгого ожидания лица/карты!
        # # # 5. Добавляем лицо человеку
        client.download_face_url(face_url, user_id)

        res_photo = client.add_photo_to_user_by_url(face_url, user_id)
        logger.debug(f"Ответ add_photo_to_user_by_url: {res_photo}")

        logger.info(f"--- УСПЕХ: Пользователь {username} [ID: {user_id}] полностью зарегистрирован ---")
    except HikvisionRequestFailed as e:
        logger.critical(f"Ошибка терминала: {str(e)}", exc_info=True)
        sys.exit(1)