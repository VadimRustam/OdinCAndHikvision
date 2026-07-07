"""Модуль для парсинга данных сотрудников BassGold. 

Данные вида:
"2dfca092-e013-11e9-435sdfadf-3863bb8069e1": {
        "iin": "1",
        "fio": "а444444444в а444к т444444ы",
        "is_active": "true",
        "department": "Цех переработки первичной руды (ЦППР)",
        "positions": "Машинист насосных установок (хвостовое хозяйство)",
        "notAYP": "true",
        "by": "true"
    },

Содержит бизнес-логику подсчета тарифов с учетом зон доставки,
веса посылки и применения промокодов.
"""

__version__ = "1.0.0"
__author__ = "Вадим Рустамович"

from collections import defaultdict
from datetime import datetime
import json
import os
import sys
import xml.etree.ElementTree as ET
import logging
import time
from typing import Any, Callable, Dict
from typing import Set
from typing import List
import requests
from requests.auth import HTTPBasicAuth
from utils import normalizeFio
from utils import measure_time
from dotenv import load_dotenv

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("ZUPIntegration")

load_dotenv(override=True)

class ZupmainRequestFailed(Exception):

    def __init__(
        self,
        endpoint,
        message,
        status_code=None,
        error_type=None,
        last_error=None,
        last_response=None
    ):
        self.endpoint = endpoint
        self.status_code = status_code
        self.error_type = error_type
        self.last_error = last_error
        self.last_response = last_response

        super().__init__(
            f"{message}\n"
            f"Endpoint: {endpoint}\n"
            f"Status: {status_code}\n"
            f"Type: {error_type}\n"
            f"Error: {last_error}\n"
            f"Response: {last_response}"
        )

    pass

class ZupmainCredentialError(Exception):
    """Не введены, либо не правильно заполненны логин/пароль от базы ZUPmain"""

    pass

class ZupmainsafeCall(Exception):
    """Ошибка при парсинге или обработке конкретного этапа данных XML."""
    def __init__(self, stage, message, original_error=None):
        self.stage = stage
        self.original_error = original_error

        super().__init__(
            f"{message}\nStage: {stage}\nError: {original_error}"
        )

class ZUPConnection:
    """Класс, отвечающий исключительно за авторизацию и выполнение HTTP-запросов."""
    
    def __init__(self, url: str = "", username: str = "", password: str = "") -> None:
        
        if not all(
            [
            url and url.strip(), 
            username and username.strip(), 
            password and password.strip(),
            ]
        ):
            logger.critical("Инициализация провалена: отсутствуют учетные данные.")
            raise ZupmainCredentialError("ip_address, username, password обязательны")

        self.url: str = url.strip().rstrip('/')
        self.username: str = username.strip()
        self.password: str = password.strip()
        self.auth = HTTPBasicAuth(self.username, self.password)

    def resetAuth(self) -> None:
        """Перевыпуск объекта авторизации."""
        logger.debug("Сброс HTTPBasicAuth")
        self.auth = HTTPBasicAuth(self.username, self.password)

    def request(self, endpoint: str, retries: int = 5):
        """Выполняет HTTP GET запрос к указанному эндпоинту с механизмом повторения."""
        url = f"{self.url}/{endpoint}"

        last_error = None
        last_response = None
        last_status = None
        error_type = None

        auth_attempted = False

        for attempt in range(1, retries + 1):

            try:
                response = requests.get(
                    url=url,
                    auth=self.auth,
                    timeout=30
                )

                status = response.status_code

                if 200 <= status < 300:
                    return response.text

                if status == 401:

                    last_status = status
                    last_response = response.text
                    error_type = "AUTH_ERROR"

                    if auth_attempted:
                        break

                    auth_attempted = True
                    self.resetAuth()
                    continue

                if 400 <= status < 500:

                    last_status = status
                    last_response = response.text
                    error_type = "CLIENT_ERROR"

                    raise ZupmainRequestFailed(
                        endpoint,
                        "Client error",
                        status_code=status,
                        error_type=error_type,
                        last_response=last_response
                    )

                if 500 <= status < 600:

                    last_status = status
                    last_response = response.text
                    error_type = "SERVER_ERROR"

                    if attempt == retries:
                        break

                    time.sleep(attempt * 10)
                    continue

                last_status = status
                last_response = response.text
                error_type = "UNKNOWN_HTTP"

                break

            except (
                requests.Timeout,
                requests.ConnectionError
            ) as e:

                last_error = e
                error_type = "NETWORK_ERROR"

                if attempt == retries:
                    break

                time.sleep(attempt * 10)
                continue

        raise ZupmainRequestFailed(
            endpoint,
            "Request failed after retries",
            status_code=last_status,
            error_type=error_type,
            last_error=last_error,
            last_response=last_response
        )


class ZUPDataParser:
    """Класс, отвечающий за бизнес-логику и парсинг XML-ответов от 1С."""
    
    def __init__(self, connection: ZUPConnection) -> None:
        self.api = connection
        self._ns = {
            'atom': 'http://www.w3.org/2005/Atom',
            'm': 'http://schemas.microsoft.com/ado/2007/08/dataservices/metadata',
            'd': 'http://schemas.microsoft.com/ado/2007/08/dataservices'
        }

    def fetch_employees(self) -> str:
        return self.api.request("Catalog_Сотрудники")

    def fetch_individuals(self) -> str:
        return self.api.request("Catalog_ФизическиеЛица")
    
    def fetch_employee_statuses(self) -> str:
        return self.api.request("InformationRegister_ДанныеСостоянийСотрудников")
    
    def fetch_current_pay_rates(self) -> str:
        return self.api.request("InformationRegister_ТекущаяТарифнаяСтавкаСотрудников")

    def fetch_user_positions(self) -> str:
        return self.api.request("Catalog_Должности")
    
    def fetch_user_departments(self) -> str:
        return self.api.request("Catalog_ПодразделенияОрганизаций")
    
    def fetch_personnel_history(self) -> str:
        return self.api.request("InformationRegister_КадроваяИсторияСотрудников")
    
    def parse_positions_catalog(self, positions: str) -> Dict[str, str]:
        """Парсит справочник должностей из XML."""
        root = ET.fromstring(positions)

        positions = {}

        for entry in root.findall("atom:entry", self._ns):
            props = entry.find('atom:content/m:properties', self._ns)

            if props is None:
                continue

            id_obj = props.find("d:Ref_Key", self._ns)
            description_obj = props.find("d:Description", self._ns)

            id_ = (id_obj.text or "") if id_obj is not None else ""
            description = (description_obj.text or "") if description_obj is not None else ""
            
            if description == "" or id_ == "":
                continue

            positions[id_] = description

        return positions 

    def parse_departments_catalog(self, department: str) -> Dict[str, str]:
        """Парсит справочник подразделений из XML."""
        root = ET.fromstring(department)

        departments = {}

        for entry in root.findall("atom:entry", self._ns):
            props = entry.find('atom:content/m:properties', self._ns)

            if props is None:
                continue

            id_obj = props.find("d:Ref_Key", self._ns)
            description_obj = props.find("d:Description", self._ns)

            if description_obj is None or id_obj is None:
                continue

            id_ = (id_obj.text or "") if id_obj is not None else ""
            description = (description_obj.text or "") if description_obj is not None else ""
            
            if description == "" or id_ == "":
                continue

            departments[id_] = description

        return departments
    
    def build_position_history(
            self, historyWorkingPeople: str, active_person_ids: Set[str]
        ) -> Dict[str, List[Dict[str, str]]]:
        """Строит историю кадровых изменений для активных физлиц."""
        idPositionsDepartment = defaultdict(list)

        root = ET.fromstring(historyWorkingPeople)

        for entry in root.findall("atom:entry", self._ns):
            props = entry.find('atom:content/m:properties', self._ns)

            if props is None:
                continue
            
            record_set_obj = props.find("d:RecordSet", self._ns)

            if record_set_obj is None:
                continue

            for prop in record_set_obj.findall("d:element", self._ns):
                
                if prop is None:
                    continue

                id_obj = prop.find("d:ФизическоеЛицо_Key", self._ns)
                date_obj = prop.find("d:Period", self._ns)
                department_obj = prop.find("d:Подразделение_Key", self._ns)
                positions_obj = prop.find("d:Должность_Key", self._ns)

                if id_obj is None or date_obj is None or positions_obj is None or department_obj is None:
                    continue

                date = (date_obj.text or "") if date_obj is not None else ""
                id_ = (id_obj.text or "") if id_obj is not None else ""
                department = (department_obj.text or "") if department_obj is not None else ""
                positions = (positions_obj.text or "") if positions_obj is not None else ""
                
                if date == "" or id_ == "" or positions == "" or department == "":
                    continue

                if id_ in active_person_ids:
                    idPositionsDepartment[id_].append({
                        "date": date,
                        "positions_id": positions,
                        "department_id": department,
                    })
        
        return idPositionsDepartment

    def get_latest_positions(
        self, peoplePhysidDepartmentPositions: Dict[str, List[Dict[str, str]]]
    ) -> Dict[str, Dict[str, str]]:
        """Выбирает актуальные подразделение и должность для каждого физлица."""
        departmentPositions = {}

        for person_id, records in peoplePhysidDepartmentPositions.items():
            if not records:
                continue

            latestRecord = max(records, key=lambda x: x["date"])

            departmentPositions[person_id] = {
                "positions_id": latestRecord["positions_id"],
                "department_id": latestRecord["department_id"],
            }

        return departmentPositions

    def enrich_with_departments_and_positions(
        self, 
        peopleDepartmentPosition: Dict[str, Dict[str, str]], 
        departmentsCatalog: Dict[str, str], 
        positionsCatalog: Dict[str, str]
    ) -> Dict[str, Dict[str, str]]:
        """Заменяет ID должностей и департаментов на их текстовые описания."""
        for _, info in peopleDepartmentPosition.items():
            department_id = info.get("department_id")
            position_id = info.get("positions_id")

            if department_id in departmentsCatalog:
                info["department"] = departmentsCatalog[department_id]

            if position_id in positionsCatalog:
                info["positions"] = positionsCatalog[position_id]

        return peopleDepartmentPosition

    def build_employee_status_history(self, statuses_xml) -> Dict[str, List[Dict[str, str]]]:
        """Собирает историю состояний (статусов) сотрудников из XML."""
        dataStatusEmployersHistory = defaultdict(list)
        root = ET.fromstring(statuses_xml)

        for entry in root.findall("atom:entry", self._ns):

            props = entry.find("atom:content/m:properties", self._ns)

            if props is None:
                continue
            
            record_set_obj = props.find("d:RecordSet", self._ns)

            if record_set_obj is not None:
                
                for prop in record_set_obj.findall("d:element", self._ns):

                    id_obj = prop.find("d:Сотрудник_Key", self._ns)
                    state_obj = prop.find("d:Состояние", self._ns)
                    start_obj = prop.find("d:Начало", self._ns)

                    id_ = (id_obj.text or "") if id_obj is not None else ""
                    state = (state_obj.text or "") if state_obj is not None else ""
                    start = (start_obj.text or "") if start_obj is not None else ""

                    dataStatusEmployersHistory[id_].append({
                        "state": state,
                        "start": start,
                    })

        return dataStatusEmployersHistory
    
    def get_active_employee_ids(self, dataStatusEmployersHistory) -> Set[str]:
        activePeopleIdState = {}
        active_ids = set()
        
        # Приоритет состояний (выше число = выше приоритет при одинаковой дате)
        STATE_PRIORITY = {
            "Увольнение": 100,
            "ОтсутствиеПоНевыясненнымПричинам": 1,
            "Болезнь": 1,
            "Работа": 0,
        }
        
        for id_, records in dataStatusEmployersHistory.items():
            if not records:
                continue
            
            # Находим максимальную дату
            max_date = max(datetime.fromisoformat(r["start"]) for r in records) # решение как это исправить
            
            # Фильтруем только события с максимальной датой
            latest_events = [r for r in records if datetime.fromisoformat(r["start"]) == max_date]
            
            # Выбираем событие с наивысшим приоритетом
            latest = max(latest_events, key=lambda r: STATE_PRIORITY.get(r["state"], 0))
            latest_state = latest["state"]

            if latest_state != "Увольнение":
                active_ids.add(id_)
                activePeopleIdState[id_] = latest_state
        
        return active_ids

    def build_active_physical_persons(self, defineWorkingPeople, employees_xml) -> Set[str]:
        """Фильтрует архивные записи и сопоставляет активных сотрудников с ID физлиц."""
        root = ET.fromstring(employees_xml)
        activePeople = set()

        for entry in root.findall('atom:entry', self._ns):
            props = entry.find('atom:content/m:properties', self._ns)

            if props is None:
                continue

            delition_obj = props.find("d:DeletionMark", self._ns)
            archive_obj = props.find("d:ВАрхиве", self._ns)
            id_obj = props.find("d:Ref_Key", self._ns)
            description_obj = props.find("d:Description", self._ns)
            physicalFace_obj = props.find("d:ФизическоеЛицо_Key", self._ns)


            delition = (delition_obj.text or "") if delition_obj is not None else ""
            archive = (archive_obj.text or "") if archive_obj is not None else ""   
            id_ = (id_obj.text or "") if id_obj is not None else ""   
            description = (description_obj.text or "") if description_obj is not None else ""   
            physicalFace = (physicalFace_obj.text or "") if physicalFace_obj is not None else ""

            if delition == "" or archive == "" or description == "":
                continue

            if delition == "true" or archive == "true":
                continue

            if id_ in defineWorkingPeople:
                activePeople.add(physicalFace)

        return activePeople

    def parse_individuals(self, information: str) -> Dict[str, Dict[str, str]]:
        """Парсит персональные данные физлиц (ИИН, ФИО)."""
        root = ET.fromstring(information)
        idiinfio = {}

        for entry in root.findall("atom:entry", self._ns):
            props = entry.find("atom:content/m:properties", self._ns)

            if props is None:
                continue

            id_obj = props.find("d:Ref_Key", self._ns)
            iin_obj = props.find("d:ИНН", self._ns)
            fio_obj = props.find("d:ФИО", self._ns)

            iin = (iin_obj.text or "") if iin_obj is not None else ""
            fio = (fio_obj.text or "") if fio_obj is not None else ""
            id_ = (id_obj.text or "") if id_obj is not None else ""

            if iin == "" or fio == "" or id_ == "":
                continue
            
            fio = normalizeFio(fio)

            idiinfio[id_] = {"iin": iin, "fio": fio}
        
        return idiinfio

    def build_active_people_profile(
            self, active_person_ids: Set[str], getiinfio: Dict[str, Dict[str, str]]
        ) -> Dict[str, Dict[str, str]]:
        """Формирует базовую структуру профиля для активных лиц."""
        result = {}
        for physicalFace in active_person_ids:
            if physicalFace in getiinfio:
                result[physicalFace] = {
                    "iin":getiinfio[physicalFace]["iin"],
                    "fio": getiinfio[physicalFace]["fio"],
                    "is_active": "true"
                }

        return result
    
    def attach_departments_and_positions(
            self, activePeopleDepartment: Dict[str, Dict[str, str]], getIdIinFios: Dict[str, Dict[str, str]]
        ) -> Dict[str, Dict[str, str]]:
        """Связывает текстовые департаменты и должности с профилями физлиц."""
        for id_, info in getIdIinFios.items():
            if id_ in activePeopleDepartment:
                info["department"] = activePeopleDepartment[id_]["department"]
                info["positions"] = activePeopleDepartment[id_]["positions"]

        return getIdIinFios
    
    def nonAYP(
            self, activePeoplesIdFioDepartments: Dict[str, Dict[str, str]]
        ) -> Dict[str, Dict[str, str]]:
        """Присваивает признак notAYP сотрудникам производственных подразделений."""
        AYP = ["АУП",
               "Отдел IT разработки",
               "Руководство",
               "Бухгалтерия",
               "Юридический отдел",
               "Отдел HR",
               "Финансовый отдел",
               "Информационно-аналитический отдел"
                ]

        for activePeoplesIdFioDepartment in activePeoplesIdFioDepartments.values():
            department = activePeoplesIdFioDepartment.get("department")
            if department and department not in AYP:
                activePeoplesIdFioDepartment["notAYP"] = "true"
        
        return activePeoplesIdFioDepartments

    def get_hazard_pay_employees(self, currentPayRate: str) -> Set[str]:
        """Выявляет физлиц, имеющих надбавку за вредные условия труда."""
        root = ET.fromstring(currentPayRate)
        allPeopleBy = set()
        
        for entry in root.findall('atom:entry', self._ns):
            props = entry.find('atom:content/m:properties', self._ns)

            if props is None:
                continue

            phys_id_obj = props.find('d:ФизическоеЛицо_Key', self._ns)
            bonus_obj = props.find('d:ТекущаяНадбавка', self._ns)

            person_id = (phys_id_obj.text or "") if phys_id_obj is not None else ""
            bonus = float(bonus_obj.text) if bonus_obj is not None else 0

            if person_id == "":
                continue

            if bonus > 0:
                allPeopleBy.add(person_id)

        return allPeopleBy
     
    def by(
            self, nonAYPs: Dict[str, Dict[str, str]], getAllPeopleBy: Set[str]
        ) -> Dict[str, Dict[str, str]]:
        """Добавляет признак надбавки за вредность (by) активным сотрудникам."""
        for nonAYP in nonAYPs:
            if nonAYP in getAllPeopleBy:
                nonAYPs[nonAYP]["by"] = "true"

        with open(r"employees.json", "w", encoding="utf-8") as f:
            json.dump(nonAYPs, f, ensure_ascii=False, indent=4)

        return nonAYPs
    
    def safeCall(self, stage: str, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Обертка для безопасного вызова шагов интеграции с логированием ошибок при парсинге."""
        try:
            return func(*args, **kwargs)

        except Exception as e:
            logger.exception("Stage failed: %s", stage)

            raise ZupmainsafeCall(
                stage=stage,
                message="Обработка данные от xml/ данных xml не удачная",
                original_error=e
            )

    @measure_time
    def main(self):
        try:
            # Получение данных из 1С
            employees_xml = self.safeCall("fetch_employees", self.fetch_employees)
            individuals_xml = self.safeCall("fetch_individuals", self.fetch_individuals)
            positions_xml = self.safeCall("fetch_user_positions", self.fetch_user_positions)
            departments_xml = self.safeCall("fetch_user_departments", self.fetch_user_departments)
            statuses_xml = self.safeCall("fetch_employee_statuses", self.fetch_employee_statuses)
            personnel_history_xml = self.safeCall("fetch_personnel_history", self.fetch_personnel_history)
            pay_rates_xml = self.safeCall("fetch_current_pay_rates", self.fetch_current_pay_rates)

            # Определение активных сотрудников
            status_history = self.safeCall(
                "build_employee_status_history",
                self.build_employee_status_history,
                statuses_xml
            ) # обработка

            active_employee_ids = self.get_active_employee_ids(status_history)

            active_person_ids = self.safeCall(
                "build_active_physical_persons",
                self.build_active_physical_persons,
                active_employee_ids,
                employees_xml
            )

            # Справочники
            positions_catalog = self.safeCall(
                "parse_positions_catalog",
                self.parse_positions_catalog,
                positions_xml
            ) # обработка

            departments_catalog = self.safeCall(
                "parse_departments_catalog",
                self.parse_departments_catalog,
                departments_xml
            ) # обработка

            # Последняя должность сотрудника
            position_history = self.safeCall(
                "build_position_history",
                self.build_position_history,
                personnel_history_xml,
                active_person_ids
            ) # обработка

            latest_positions = self.get_latest_positions(position_history)

            employee_positions = self.enrich_with_departments_and_positions(
                latest_positions,
                departments_catalog,
                positions_catalog,
            )

            # Профили сотрудников
            individuals = self.safeCall(
                "parse_individuals",
                self.parse_individuals,
                individuals_xml
            ) # обработка

            employees = self.build_active_people_profile(
                active_person_ids,
                individuals,
            )

            employees = self.attach_departments_and_positions(
                employee_positions,
                employees,
            )

            # Бизнес-признаки
            employees = self.nonAYP(employees)

            hazard_pay_employees = self.get_hazard_pay_employees(pay_rates_xml)

            employees = self.by(
                employees,
                hazard_pay_employees,
            )

            return employees
    
        except ZupmainRequestFailed as e:
            logger.exception(f"Error occurred while fetching data: {e}")
            

if __name__ == "__main__":

    try:

        zup_url=os.getenv("ZUP_URL")
        zup_username=os.getenv("ZUP_USERNAME")
        zup_password=os.getenv("ZUP_PASSWORD")

        connectionZUPmain = ZUPConnection(
            url=zup_url,
            username=zup_username,
            password=zup_password
        )
        clientZUPmain = ZUPDataParser(
            connection=connectionZUPmain,
        )
    except ZupmainCredentialError as e:
        logger.critical("Данные логина, пароля, url либо не заполненны, либо не верные")
            
    try:
        data = clientZUPmain.main()
    except Exception as e:
        logger.error(f"Ошибка выполнения: {e}")