"""Guide the mechanic through verified YaMZ-236M2 readings and fluid checks."""
import re

from .tools import fluid_check, oil_pressure


MOVEMENT_VERDICT = "Возможность перегона по этим данным не подтверждена."
MOVEMENT_LIMITS = (
    "Прямой запрет из руководства здесь не установлен. Возможность перегона определяет механик после очной проверки."
)
MEASUREMENT_GUIDE = re.compile(
    r"(?:условия замера давления(?: масла)?|"
    r"(?:как|когда|при каких условиях|на каких оборотах) "
    r"(?:измер|замер|мер|провер)\w* давлени[ея](?: масла)?)"
)


class MeasurementAgent:
    def __init__(self, manual_agent):
        self.manual_agent = manual_agent

    @staticmethod
    def supports(reply):
        diagnosis = reply["ml_observations"]
        return (
            diagnosis["status"] == "anomaly"
            and diagnosis["engine_model"] == "ЯМЗ-236М2"
            and diagnosis["anomaly_type"] == "F02"
        )

    def first_check(self, reply, trace, history=()):
        if not self.supports(reply):
            return False
        source = self._add_reference(reply, fluid_check.QUERY, fluid_check.SOURCE, trace)
        if source is None:
            return False
        reply.setdefault("intent", "search")
        fluids = next((row[5] for row in reversed(history) if len(row) > 5 and row[5] is not None), {})
        reply.setdefault("fluid_check", fluids)
        reply["checks"].append(dict(text=source["text"].splitlines()[0], fragment_id=fluid_check.SOURCE))
        return True

    def respond(self, reply, question, normalized, history, trace, *, movement=False, next_step=False):
        if not self.supports(reply):
            return False

        turns = ((row[0], row[3][0]["text"] if len(row) > 3 and row[3] else "",
                  row[5] if len(row) > 5 else None) for row in history)
        fluids, gauge_expected = fluid_check.restore(turns)
        if normalized == "достаточно" and fluids is not None:
            reply.update(intent="clarify", fluid_check=fluids, clarifying_question=
                         "Вы про уровни масла и охлаждающей жидкости или на этом остановимся?")
            return True

        if not movement and MEASUREMENT_GUIDE.fullmatch(normalized):
            self._measurement_guide(reply, trace)
            return True

        pressure_turn = next((row for row in reversed(history) if len(row) > 4 and row[4] is not None), None)
        previous_pressure = pressure_turn[4] if pressure_turn is not None else None
        reading = oil_pressure.reports_value(question)
        values = (oil_pressure.readings(question, previous_pressure)
                  if movement or reading or "?" not in question else previous_pressure or {})
        yes_or_no = normalized in {"да", "нет"}
        continuation = pressure_turn is not None and (
            (yes_or_no and pressure_turn is history[-1]) or (not yes_or_no and values != previous_pressure)
        )
        report = fluid_check.report(question, fluids, gauge_expected=gauge_expected)
        state = report if report is not None else fluids
        guide = fluid_check.wants_next_check(question) or (fluids is not None and next_step)
        refer_to_check = fluids and re.fullmatch(r"(?:а )?тебя (?:спросить|и спрашиваю)(?: надо)?", normalized)
        measured = reading or continuation
        gauge = (state or {}).get("gauge")
        if not movement and (report is not None or guide or refer_to_check or (gauge and measured)):
            if gauge == "faulty" or ((fluids or {}).get("gauge") == "faulty" and not reading):
                values = {}
            elif not measured:
                values = previous_pressure or {}
            self._fluid_reply(reply, state or {}, values, trace)
            return True
        if movement or reading or continuation:
            if report is not None:
                reply["fluid_check"] = report
            travel_context = movement or (pressure_turn is not None and pressure_turn[1].startswith(MOVEMENT_VERDICT))
            self._pressure_reply(reply, values, trace, movement=travel_context)
            return True

        return False

    def _add_reference(self, reply, query, fragment_id, trace):
        diagnosis = reply["ml_observations"]
        hits = self.manual_agent.search(query, diagnosis, trace)
        source = next((hit for hit in hits if hit["fragment_id"] == fragment_id), None)
        if source is not None:
            reference = self.manual_agent.manuals.reference(fragment_id, diagnosis["engine_model"])
            reply["references"].append(reference)
        return source

    def _fluid_reply(self, reply, state, pressure, trace):
        reply["fluid_check"] = state
        if pressure or state.get("gauge") == "faulty":
            reply["pressure_measurement"] = pressure
        checks, clarification = fluid_check.next_action(state, pressure)
        fragments = dict.fromkeys(fragment for fragment, _ in checks) or {fluid_check.SOURCE: None}
        for fragment in fragments:
            query = oil_pressure.QUERY if fragment == oil_pressure.SOURCE else fluid_check.QUERY
            if self._add_reference(reply, query, fragment, trace) is None:
                reply.update(intent="clarify", clarifying_question="Уточните условия проверки. Применимый раздел руководства не найден.")
                return
        reply.update(intent="search" if checks else "clarify", clarifying_question=clarification,
                     checks=[dict(text=text, fragment_id=fragment) for fragment, text in checks])

    def _pressure_reply(self, reply, values, trace, *, movement):
        verdict = (MOVEMENT_VERDICT + " Приоритет заявки не является запретом на движение."
                   if movement else "Сверка давления масла с нормативом.")
        reply.update(intent="movement" if movement else "measurement", pressure_measurement=values, verdict=verdict)
        if movement:
            pressure = values.get("pressure")
            observation = (f"Со слов механика, давление масла {pressure.replace('.', ',')} кПа."
                           if pressure is not None else "Для оценки нужны фактическое давление и условия замера.")
            reply["movement_limitations"] = f"{observation} {MOVEMENT_LIMITS}"
        source = self._add_reference(reply, oil_pressure.QUERY, oil_pressure.SOURCE, trace)
        if source is None:
            reply["clarifying_question"] = "Не удалось найти применимый норматив в руководстве."
            return
        result, reply["clarifying_question"] = oil_pressure.compare(values)
        if result:
            reply["checks"].append(dict(text=result, fragment_id=oil_pressure.SOURCE))
        reply["pressure_reference_text"] = source["text"]

    def _measurement_guide(self, reply, trace):
        source = self._add_reference(reply, oil_pressure.QUERY, oil_pressure.SOURCE, trace)
        if source is None:
            reply.update(intent="clarify", clarifying_question="Не удалось найти условия замера для этого двигателя.")
            return
        reply.update(intent="measurement", verdict="Условия замера давления масла.",
                     clarifying_question="Если есть показание, укажите давление с единицей, обороты и прогрев.")
        reply["checks"].append(dict(text=source["text"], fragment_id=oil_pressure.SOURCE))
