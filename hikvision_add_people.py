import logging
from pathlib import Path
import random
import time
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional
import requests
from requests.auth import HTTPDigestAuth
import json
import sys
import os

logger = logging.getLogger("HikvisionClient")
logger.setLevel(logging.DEBUG)

class HikvisionRequestFailed(Exception):
    """Общая ошибка запроса после всех попыток"""
    def __init__(self, url: str, message: str, last_error: str = None, last_response: str = None):
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

        self.auth: Optional[HTTPDigestAuth] = HTTPDigestAuth(self.username, self.password)

    def resetAuth(self) -> None:
        """Сброс и повторная инициализация Digest авторизации."""
        logger.debug("Обновление/сброс объекта HTTPDigestAuth.")
        self.auth = HTTPDigestAuth(self.username, self.password)

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
                response = requests.request(
                    method=method, url=url, auth=self.auth, timeout=30, **kwargs
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
                    
                    self.resetAuth()
                    time.sleep(1)  # Небольшая пауза перед повтором
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
        self.__ns: Dict[str, str] = {
            "ns": "http://www.isapi.org/ver20/XMLSchema",
        }

    def grantDailyAccessToAll(self, userIds: List[str]) -> None:
        """Выдать каждому пользователю 1 доступ."""
        try:
            for userId in userIds:
                self.setMaxVisits(userId)
            logger.info("Суточный доступ успешно обновлен для всех пользователей.")
        except Exception as e:
            logger.error(f"Ошибка при выдаче суточного доступа: {e}")

    def setMaxVisits(self, employee_no: str) -> None:
        """Установка ограничений на открытие дверей (1 раз в сутки)."""
        url = f"{self.api.base_url}/AccessControl/UserInfo/Modify?format=json"
        payload = {
            "UserInfo": {
                "employeeNo": str(employee_no),
                "maxOpenDoorTime": 1,
                "openDoorTime": 0,
            }
        }
        self.api.request("PUT", url, headers=self.api.headers_json, json=payload)
    
    def addPhotoToUserByUrl(self, faceURL: str, employee_no: str) -> str:
        """Привязка фотографии к пользователю по URL."""
        if not faceURL:
            raise ValueError("Передан пустой faceURL")

        url = f"{self.api.base_url}/Intelligent/FDLib/FDSetUp?format=json"
        payload = {
            "faceLibType": "blackFD",
            "FDID": "1",
            "FPID": str(employee_no),
            "bornTime": "1990-01-01",
            "saveFacePic": True,
            "faceURL": faceURL,
        }

        response = self.api.request("PUT", url, headers=self.api.headers_json, json=payload)
        return response.text

    def addCard(self, card_number: str, employee_no: str) -> str:
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
            if self.extractHikvisionStatusCode(e.last_response) == 6:
                logger.warning("Карта уже добавлена.")
                raise CardAlreadyAssignedError(card_number) from e
            raise
        return response.text

    def extractHikvisionStatusCode(self, response: str) -> Optional[int]:
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
            if isinstance(data, dict) and "statusCode" in data:
                return int(data["statusCode"])
        except (json.JSONDecodeError, ValueError, TypeError):
            pass

        try:
            ns = {
            'ns': "http://www.hikvision.com/ver10/XMLSchema",
            }
            root = ET.fromstring(response)
            status = root.find("ns:statusCode", ns)
            if status is not None and status.text is not None:
                return int(status.text.strip())
        except (ET.ParseError, ValueError):
            pass

        
    def getCardnoByTerminal(self) -> str:
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
            if self.extractHikvisionStatusCode(e.last_response) == 3:
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

    def getFaceTerminal(self) -> str:
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
            if self.extractHikvisionStatusCode(e.last_response) == 3:
                logger.warning("Лицо не было распознано терминалом в течение отведенного времени.")
                raise FaceCaptureTimeoutError() from e
            raise

        try:
            root = ET.fromstring(response.text)
            face_url_node = root.find("ns:faceDataUrl", self.__ns)

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
        
    def addUser(self, id_people: str, username: str) -> str:
        """Добавление учетной записи человека (без фото и карты)."""
        request_url = f"{self.api.base_url}/AccessControl/UserInfo/SetUp?format=json"
        payload = {
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
    def generaitId(self) -> str:
        """Генерация уникального 7-значного ID."""
        return str(random.randint(1000000, 9999999))

    def cardDelete(self, card_number: str, employee_no: str) -> None:
        request_url = f"{self.api.base_url}/AccessControl/CardInfo/Delete?format=json"
        data = {
            "CardInfoDelCond": {
                "employeeNo": employee_no,
                "cardNo": card_number, # "cardType": "normalCard", "leaderCard": "1" параметры какие я использовал, пусть будут
            }
        }
        self.api.request("PUT", request_url, headers=self.api.headers_json, json=data)
    
    def deleteAllCardsForUser(self, userId: str, cards: List[str]) -> None:
        if cards is None or cards == []:
            logger.info(f"У пользователя с ИИН {userId} нет карт для удаления.")
            return
        logger.info(f"У пользователя с ИИН {userId} карта {cards} ---")
        for card in cards:
            self.cardDelete(card, userId)
            
    # Оптимизированный поиск карт конкретного пользователя
    def getAllUserCards(self, userId: str) -> Optional[List[str]]:
        request_url = f'{self.api.base_url}/AccessControl/CardInfo/Search?format=json'
        data = {
            "CardInfoSearchCond": {
                "searchID": "1",
                "maxResults": 10,
                "searchResultPosition": 0,
                "EmployeeNoList": [{"employeeNo": str(userId)}]
            }
        }  
        response = self.api.request("POST", request_url, headers=self.api.headers_json, json=data)
        cards = response.json().get("CardInfoSearch", {}).get("CardInfo", [])
        return [c.get("cardNo") for c in cards if c.get("cardNo")]
    
    def downloadFaceUrl(self, url: str, userId: str) -> None:
        print(f"Downloading face image for user: {userId} url {url}")
        img_response = self.api.request("GET", url, headers=self.api.headers_json)
        if img_response.status_code == 200:
            file_path = os.path.join(r"C:\Users\Вадим\Desktop\Hickvision&Odoo\prod\photo", f"{userId}.jpg")
            with open(file_path, "wb") as f:
                f.write(img_response.content)

        return file_path
    
    def addPhotoByPeoplePhoto(self, employee_no: str, photo_path: str):
        url = f'{self.base_url}/Intelligent/FDLib/FaceDataRecord?format=json'
        payload = {
            "faceLibType": "blackFD",
            "FDID": "1",
            "FPID": employee_no,
            "bornTime": "1990-01-01",
            "saveFacePic": True,
        }

        files = {
            "faceURL": (None, json.dumps(payload), "application/json"),
            "img":     ("facePic.jpg", Path(photo_path).read_bytes(), "image/jpeg"),
        }

        response = requests.post(
            url,
            auth=self.api.auth,
            data=payload,
            files=files,
        )

        return response.text
    
if __name__ == "__main__":
    # Настройки для проверки

    try:

        ip_address=os.getenv("HIKVISION_IP")
        username=os.getenv("HIKVISION_USERNAME")
        password=os.getenv("HIKVISION_PASSWORD")

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
        client.grantDailyAccessToAll([])
        username = "ФИО"
        userId = "1" # метод для теста, будет ИИН человека вводится, уникальный идентификатор
        
        logger.info(f"--- НАЧАЛО ПРОЦЕССА: Создание пользователя {username} с ID {userId} ---")

        cardIds = client.getAllUserCards(userId)
        client.deleteAllCardsForUser(userId, cardIds)

        # 1. Считываем карту
        try:
            cardId = client.getCardnoByTerminal()
            logger.debug(f"Ответ getFaceTerminal: {cardId}")
        except CardCaptureTimeoutError as e:
            logger.warning(f"{e}")
            # вывод в Odoo: odoo.User("Карта не была приложена к терминалу. Попробуйте еще раз.")
            sys.exit(1)
        
        # 3. Добавляем пользователя
        res_user = client.addUser(userId, username)
        logger.debug(f"Ответ addUser: {res_user}")

        # 4. Добавляем карту
        try:
            res_card = client.addCard(cardId, userId)
            logger.debug(f"Ответ addCard: {res_card}")
        except CardAlreadyAssignedError as e:
            logger.warning(f"Карта с номером {cardId} уже назначена другому пользователю: {e}")
            # вывод в Odoo odoo.User("Карта уже занята человеком")
            sys.exit(1)
                
        try:
            faceURL = client.getFaceTerminal()
            logger.debug(f"Ответ getFaceTerminal: {faceURL}")
        except FaceCaptureTimeoutError as e:    
            logger.warning(f"{e}")
            # вывод в Odoo: odoo.User("Лицо не было распознано терминалом. Попробуйте еще раз.")
            sys.exit(1)
        
        # # # метод self.api.request автоматически сделает resetAuth(),
        # # # если сессия здесь упадет по 401 ошибке из-за долгого ожидания лица/карты!
        # # # 5. Добавляем лицо человеку
        client.downloadFaceUrl(faceURL, userId)

        res_photo = client.addPhotoToUserByUrl(faceURL, userId)
        logger.debug(f"Ответ addPhotoToUserByUrl: {res_photo}")

        logger.info(f"--- УСПЕХ: Пользователь {username} [ID: {userId}] полностью зарегистрирован ---")
    except HikvisionRequestFailed as e:
        logger.critical(f"Ошибка терминала: {str(e)}", exc_info=True)
        sys.exit(1)