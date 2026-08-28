"""Importer tests. Every fixture is marked TEST-DEMO and never leaves this process."""
import contextlib, io, json, os, tempfile, unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

os.environ.setdefault("VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("WHATSAPP_TOKEN", "test-whatsapp-token")
os.environ.setdefault("PHONE_NUMBER_ID", "123456")
os.environ.setdefault("APP_SECRET", "test-app-secret")
import app
import inventory_import as importer

NOW = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
FRESH = (NOW - timedelta(days=1)).isoformat()
STALE = (NOW - timedelta(days=30)).isoformat()


def demo(**overrides):
    """A valid TEST-DEMO listing; override one field to build each failure case."""
    record = {"public_reference": "TEST-DEMO-001", "operation": "compra", "property_type": "departamento",
              "district": "Surco", "price_amount": 200000, "currency": "USD", "bedrooms": 2,
              "parking_spaces": 1, "lifecycle_state": "active_confirmed",
              "approved_at": FRESH, "availability_confirmed_at": FRESH}
    record.update(overrides)
    return {key: value for key, value in record.items() if value is not None}


def stored(**overrides):
    row = demo(**overrides)
    row.setdefault("property_id", "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    return row


def actions(plans):
    return [plan["action"] for plan in plans]


class NormalizationTests(unittest.TestCase):
    def test_property_type_is_canonicalized_so_searches_can_match(self):
        record, errors = importer.normalize_record(demo(property_type="Apartamento"))
        self.assertEqual((record["property_type"], errors), ("departamento", []))

    def test_a_listing_for_sale_becomes_the_compra_side_of_the_search(self):
        """Portals say 'venta'; inventory.operation is the client's side of the deal."""
        record, errors = importer.normalize_record(demo(operation="Venta"))
        self.assertEqual((record["operation"], errors), ("compra", []))
        self.assertEqual(importer.validate_record(record), [])

    def test_a_rental_listing_stays_alquiler(self):
        record, _ = importer.normalize_record(demo(operation="Alquiler"))
        self.assertEqual(record["operation"], "alquiler")

    def test_an_unmappable_operation_is_still_rejected(self):
        record, _ = importer.normalize_record(demo(operation="permuta"))
        self.assertTrue(any("operation" in e for e in importer.validate_record(record)))

    def test_currency_and_operation_are_case_normalized(self):
        record, _ = importer.normalize_record(demo(operation="Compra", currency="usd"))
        self.assertEqual((record["operation"], record["currency"]), ("compra", "USD"))

    def test_numbers_and_features_survive_a_csv_round_trip(self):
        record, errors = importer.normalize_record(
            {**demo(), "price_amount": "185,000", "bedrooms": "3", "features": "vista al mar; ascensor"})
        self.assertEqual(errors, [])
        self.assertEqual((record["price_amount"], record["bedrooms"]), (185000.0, 3))
        self.assertEqual(record["features"], ["vista al mar", "ascensor"])

    def test_unparsable_numbers_are_reported_not_guessed(self):
        _, errors = importer.normalize_record(demo(price_amount="a convenir"))
        self.assertTrue(any("price_amount" in error for error in errors))


class ValidationTests(unittest.TestCase):
    def check(self, **overrides):
        record, errors = importer.normalize_record(demo(**overrides))
        return errors + importer.validate_record(record)

    def test_a_complete_listing_passes(self):
        self.assertEqual(self.check(), [])

    def test_missing_required_field_is_rejected(self):
        self.assertTrue(any("district: required" in error for error in self.check(district=None)))

    def test_non_positive_price_is_rejected_as_invalid_not_as_missing(self):
        errors = self.check(price_amount=0)
        self.assertIn("price_amount: must be greater than 0", errors)
        self.assertNotIn("price_amount: required", errors)

    def test_unsupported_currency_is_rejected(self):
        self.assertTrue(any("currency" in error for error in self.check(currency="EUR")))

    def test_a_seller_search_never_reaches_inventory(self):
        """The 'venta' that must not reach inventory is the client's, not the listing's.

        A listing marked 'venta' is stock for a 'compra' search and is normalized as
        such; a client who wants to SELL is stopped upstream, by inventory_ready.
        """
        self.assertFalse(app.inventory_ready({"operation": "venta", "property_type": "departamento",
                                              "districts": ["Surco"], "budget_max": 1, "currency": "USD",
                                              "bedrooms": 2}))

    def test_unknown_lifecycle_state_is_rejected(self):
        self.assertTrue(any("lifecycle_state" in error for error in self.check(lifecycle_state="publicado")))

    def test_visible_state_without_approval_or_availability_is_rejected(self):
        errors = self.check(approved_at=None, availability_confirmed_at=None)
        self.assertTrue(any("also needs approved_at" in error for error in errors))

    def test_negative_bedrooms_is_rejected(self):
        self.assertTrue(any("bedrooms" in error for error in self.check(bedrooms=-1)))


class PlanTests(unittest.TestCase):
    def test_new_reference_is_an_insert(self):
        plans = importer.plan_records([demo()], {}, now=NOW)
        self.assertEqual(actions(plans), ["INSERT"])

    def test_rerunning_the_same_source_changes_nothing(self):
        existing = {"TEST-DEMO-001": stored()}
        plans = importer.plan_records([demo()], existing, now=NOW)
        self.assertEqual(actions(plans), ["NO-OP"])
        self.assertEqual(plans[0]["changes"], {})

    def test_changed_attribute_is_a_scoped_update(self):
        existing = {"TEST-DEMO-001": stored()}
        plans = importer.plan_records([demo(price_amount=190000)], existing, now=NOW)
        self.assertEqual(actions(plans), ["UPDATE"])
        self.assertEqual(plans[0]["changes"], {"price_amount": 190000.0})

    def test_duplicate_inside_the_source_is_rejected_not_applied_twice(self):
        plans = importer.plan_records([demo(), demo(price_amount=1)], {}, now=NOW)
        self.assertEqual(actions(plans), ["INSERT", "REJECT"])
        self.assertTrue(any("duplicated inside the source" in error for error in plans[1]["errors"]))

    def test_invalid_record_never_reaches_an_action(self):
        plans = importer.plan_records([demo(currency="EUR")], {}, now=NOW)
        self.assertEqual(actions(plans), ["REJECT"])
        self.assertEqual(plans[0]["changes"], {})

    def test_insert_without_approval_warns_it_will_stay_invisible(self):
        plans = importer.plan_records([demo(lifecycle_state="draft", approved_at=None,
                                            availability_confirmed_at=None)], {}, now=NOW)
        self.assertEqual(actions(plans), ["INSERT"])
        self.assertTrue(plans[0]["gaps"])

    def test_source_name_is_recorded_for_traceability(self):
        plans = importer.plan_records([demo()], {}, now=NOW, source_name="ficha-whatsapp")
        self.assertEqual(plans[0]["record"]["source_name"], "ficha-whatsapp")

    def test_dry_run_planning_writes_nothing(self):
        store = Mock()
        importer.plan_records([demo()], {}, now=NOW)
        store.insert.assert_not_called()
        store.update.assert_not_called()


class AvailabilitySemanticsTests(unittest.TestCase):
    """Phase 3/4: availability is an operator claim, never a side effect."""

    def test_an_ordinary_update_does_not_renew_availability(self):
        existing = {"TEST-DEMO-001": stored(availability_confirmed_at=STALE)}
        plans = importer.plan_records([demo(price_amount=190000)], existing, now=NOW)
        self.assertEqual(plans[0]["action"], "UPDATE")
        self.assertNotIn(importer.AVAILABILITY, plans[0]["changes"])

    def test_running_the_importer_alone_never_renews_availability(self):
        existing = {"TEST-DEMO-001": stored(availability_confirmed_at=STALE)}
        plans = importer.plan_records([demo(availability_confirmed_at=STALE)], existing, now=NOW)
        self.assertEqual(plans[0]["action"], "NO-OP")

    def test_confirm_availability_touches_only_that_field(self):
        existing = {"TEST-DEMO-001": stored(availability_confirmed_at=STALE)}
        plans = importer.plan_records([demo(availability_confirmed_at=None)], existing,
                                      now=NOW, confirm_availability=True)
        self.assertEqual(plans[0]["action"], "UPDATE")
        self.assertEqual(list(plans[0]["changes"]), [importer.AVAILABILITY])
        self.assertEqual(plans[0]["changes"][importer.AVAILABILITY], NOW.isoformat())

    def test_republishing_an_unchanged_property_is_a_noop(self):
        """--publish twice must not re-stamp an approval nobody renewed."""
        loaded = stored(lifecycle_state="active_confirmed", approved_at=FRESH,
                        availability_confirmed_at=FRESH)
        plans = importer.plan_records([demo(lifecycle_state="draft", approved_at=None,
                                            availability_confirmed_at=None)],
                                      {"TEST-DEMO-001": loaded}, now=NOW, publish=True)
        self.assertEqual(plans[0]["action"], "NO-OP")

    def test_republishing_with_a_stated_confirmation_does_update_it(self):
        loaded = stored(lifecycle_state="active_confirmed", approved_at=FRESH,
                        availability_confirmed_at=STALE)
        again = "2026-08-28T09:30:00+00:00"
        plans = importer.plan_records([demo(availability_confirmed_at=again)],
                                      {"TEST-DEMO-001": loaded}, now=NOW, publish=True)
        self.assertEqual(plans[0]["changes"], {importer.AVAILABILITY: again})

    def test_publish_keeps_the_confirmation_time_that_was_actually_stated(self):
        """Stamping "now" would extend the seven-day window past the real check."""
        confirmed = "2026-08-28T09:30:00+00:00"
        plans = importer.plan_records([demo(lifecycle_state="draft", approved_at=None,
                                            availability_confirmed_at=confirmed)], {},
                                      now=NOW, publish=True)
        self.assertEqual(plans[0]["record"][importer.AVAILABILITY], confirmed)

    def test_publish_is_an_explicit_claim_that_sets_state_approval_and_availability(self):
        plans = importer.plan_records([demo(lifecycle_state="draft", approved_at=None,
                                            availability_confirmed_at=None)], {}, now=NOW, publish=True)
        record = plans[0]["record"]
        self.assertEqual(record["lifecycle_state"], "active_confirmed")
        self.assertEqual(record[importer.AVAILABILITY], NOW.isoformat())
        self.assertEqual(plans[0]["gaps"], [])


class SevenDayRuleTests(unittest.TestCase):
    """The window belongs to app.py; these prove the importer agrees with it."""

    def test_a_recently_confirmed_property_is_visible(self):
        self.assertTrue(app.property_is_eligible(stored(availability_confirmed_at=FRESH), NOW))

    def test_an_expired_property_is_not_visible(self):
        self.assertFalse(app.property_is_eligible(stored(availability_confirmed_at=STALE), NOW))

    def test_the_boundary_is_exactly_the_configured_window(self):
        edge = (NOW - timedelta(days=app.INVENTORY_VERIFICATION_DAYS)).isoformat()
        just_past = (NOW - timedelta(days=app.INVENTORY_VERIFICATION_DAYS, seconds=1)).isoformat()
        self.assertTrue(app.property_is_eligible(stored(availability_confirmed_at=edge), NOW))
        self.assertFalse(app.property_is_eligible(stored(availability_confirmed_at=just_past), NOW))

    def test_reconfirming_makes_an_expired_property_eligible_again(self):
        existing = {"TEST-DEMO-001": stored(availability_confirmed_at=STALE)}
        self.assertFalse(app.property_is_eligible(existing["TEST-DEMO-001"], NOW))
        plans = importer.plan_records([demo(availability_confirmed_at=None)], existing,
                                      now=NOW, confirm_availability=True)
        revived = {**existing["TEST-DEMO-001"], **plans[0]["changes"]}
        self.assertTrue(app.property_is_eligible(revived, NOW))

    def test_expiry_hides_the_property_but_never_deletes_it(self):
        row = stored(availability_confirmed_at=STALE)
        self.assertFalse(app.property_is_eligible(row, NOW))
        self.assertEqual(row["public_reference"], "TEST-DEMO-001")
        self.assertEqual(row["lifecycle_state"], "active_confirmed")

    def test_an_inactive_property_is_never_visible_even_when_freshly_confirmed(self):
        self.assertFalse(app.property_is_eligible(stored(lifecycle_state="inactive"), NOW))


class FreshnessReportTests(unittest.TestCase):
    def test_rows_are_bucketed_by_the_matcher_window(self):
        window = app.INVENTORY_VERIFICATION_DAYS
        vigente = (NOW - timedelta(days=1)).isoformat()
        por_vencer = (NOW - timedelta(days=window - 1)).isoformat()
        vencida = (NOW - timedelta(days=window + 1)).isoformat()
        self.assertEqual(importer.freshness_status({"availability_confirmed_at": vigente}, NOW), "vigente")
        self.assertEqual(importer.freshness_status({"availability_confirmed_at": por_vencer}, NOW), "por_vencer")
        self.assertEqual(importer.freshness_status({"availability_confirmed_at": vencida}, NOW), "vencida")

    def test_a_property_never_confirmed_counts_as_expired(self):
        self.assertEqual(importer.freshness_status({}, NOW), "vencida")


class SourceReadingTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tempdir.cleanup()

    def write(self, name, content):
        path = os.path.join(self.tempdir.name, name)
        with open(path, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
        return path

    def test_json_list_and_wrapped_object_read_the_same(self):
        plain = self.write("a.json", json.dumps([demo()]))
        wrapped = self.write("b.json", json.dumps({"properties": [demo()]}))
        self.assertEqual(importer.read_records(plain), importer.read_records(wrapped))

    def test_csv_is_accepted_for_the_same_contract(self):
        path = self.write("c.csv", "public_reference,operation,property_type,district,price_amount,currency\n"
                                   "TEST-DEMO-002,compra,departamento,Surco,150000,USD\n")
        records = importer.read_records(path)
        self.assertEqual(records[0]["public_reference"], "TEST-DEMO-002")
        record, errors = importer.normalize_record(records[0])
        self.assertEqual(errors + importer.validate_record(record), [])


class ApplyTests(unittest.TestCase):
    def test_only_inserts_and_updates_are_sent_and_nothing_is_deleted(self):
        existing = {"TEST-DEMO-001": stored()}
        plans = importer.plan_records(
            [demo(price_amount=190000), demo(public_reference="TEST-DEMO-002"), demo(public_reference=None)],
            existing, now=NOW)
        self.assertEqual(actions(plans), ["UPDATE", "INSERT", "REJECT"])
        store = Mock()
        result = importer.apply_plans(store, plans)
        self.assertEqual(result, {"inserted": 1, "updated": 1})
        self.assertEqual(store.insert.call_args.args[0][0]["public_reference"], "TEST-DEMO-002")
        self.assertEqual(store.update.call_args.args[0], "TEST-DEMO-001")
        self.assertFalse(hasattr(store, "delete") and store.delete.called)


class ReconfirmationScopeTests(unittest.TestCase):
    def test_reconfirming_an_unknown_reference_is_rejected_not_created(self):
        plans = importer.plan_records([{"public_reference": "TEST-DEMO-404"}], {},
                                      now=NOW, confirm_availability=True)
        self.assertEqual(plans[0]["action"], "REJECT")
        self.assertTrue(any("no da de alta" in error for error in plans[0]["errors"]))

    def test_reconfirmation_only_needs_the_reference(self):
        existing = {"TEST-DEMO-001": stored(availability_confirmed_at=STALE)}
        plans = importer.plan_records([{"public_reference": "TEST-DEMO-001"}], existing,
                                      now=NOW, confirm_availability=True)
        self.assertEqual(plans[0]["action"], "UPDATE")
        self.assertEqual(list(plans[0]["changes"]), [importer.AVAILABILITY])


class CommandLineTests(unittest.TestCase):
    """The dry-run must be the default and must never reach the store."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = Mock()
        self.store.by_reference.return_value = {}

    def tearDown(self):
        self.tempdir.cleanup()

    def run_cli(self, records, *flags):
        path = os.path.join(self.tempdir.name, "demo.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(records, handle)
        buffer = io.StringIO()
        with patch.object(importer, "_store_from_env", return_value=self.store),              contextlib.redirect_stdout(buffer):
            code = importer.main([path, *flags])
        return code, buffer.getvalue()

    def test_default_run_is_a_dry_run_that_writes_nothing(self):
        code, output = self.run_cli([demo()])
        self.assertEqual(code, 0)
        self.assertIn("DRY-RUN", output)
        self.assertIn("INSERT", output)
        self.store.insert.assert_not_called()
        self.store.update.assert_not_called()

    def test_dry_run_reports_each_rejected_record_and_exits_nonzero(self):
        code, output = self.run_cli([demo(currency="EUR")])
        self.assertEqual(code, 1)
        self.assertIn("REJECT", output)
        self.assertIn("currency", output)
        self.store.insert.assert_not_called()

    def test_apply_refuses_while_any_record_is_invalid(self):
        code, output = self.run_cli([demo(), demo(public_reference="TEST-DEMO-002", price_amount=-5)], "--apply")
        self.assertEqual(code, 1)
        self.assertIn("corrige la fuente", output)
        self.store.insert.assert_not_called()

    def test_apply_writes_only_when_every_record_is_valid(self):
        code, output = self.run_cli([demo()], "--apply")
        self.assertEqual(code, 0)
        self.assertIn("1 altas", output)
        self.store.insert.assert_called_once()


def urbania(**overrides):
    """The real Urbania listing, as a third_party record. Never applied anywhere."""
    record = {"public_reference": "ARZ-000001", "operation": "venta", "property_type": "departamento",
              "district": "Lince", "zone": "Lobatón", "price_amount": 550000, "currency": "PEN",
              "bedrooms": 2, "bathrooms": 2, "parking_spaces": 1,
              "area_total_m2": 76.82, "area_built_m2": 58.75, "terrace_area_m2": 18.07,
              "provenance": "third_party", "source_name": "urbania",
              "source_reference": "urbania:150921307",
              "source_url": "https://urbania.pe/inmueble/clasificado/veclapin-venta-de-departamento-en-lobaton-lince-2-dormitorios-ascensor-150921307",
              "advertiser_name": "INMOBILIARIA PUGA", "observed_at": FRESH}
    record.update(overrides)
    return {k: v for k, v in record.items() if v is not None}


class ProvenanceGovernanceTests(unittest.TestCase):
    """1, 2, 3, 5, 6: a borrowed listing must never pass as ARENZ stock."""

    def clean(self, **over):
        record, errors = importer.normalize_record(urbania(**over))
        return record, errors + importer.validate_record(record)

    def test_a_third_party_listing_is_not_confused_with_own_stock(self):
        record, errors = self.clean()
        self.assertEqual(errors, [])
        self.assertEqual(record["provenance"], "third_party")
        self.assertNotEqual(record["provenance"], "own")
        own, _ = importer.normalize_record(demo())
        self.assertEqual(own.get("provenance", "own"), "own")

    def test_a_third_party_listing_without_its_trail_is_rejected(self):
        for field in ("source_name", "source_reference", "source_url", "observed_at"):
            _, errors = self.clean(**{field: None})
            self.assertTrue(any(field in error for error in errors), field)

    def test_an_unknown_provenance_is_rejected(self):
        _, errors = self.clean(provenance="propio")
        self.assertTrue(any("provenance" in error for error in errors))

    def test_reading_the_source_never_becomes_availability_confirmation(self):
        record, errors = self.clean()
        self.assertEqual(errors, [])
        self.assertEqual(record["observed_at"], FRESH)
        self.assertNotIn(importer.AVAILABILITY, record)
        plans = importer.plan_records([urbania()], {}, now=NOW)
        self.assertEqual(plans[0]["action"], "INSERT")
        self.assertNotIn(importer.AVAILABILITY, plans[0]["changes"])
        self.assertFalse(app.property_is_eligible(record, NOW))

    def test_a_third_party_listing_cannot_be_shown_while_unverified(self):
        _, errors = self.clean(lifecycle_state="active_confirmed", approved_at=FRESH,
                               availability_confirmed_at=FRESH)
        self.assertTrue(any("cannot be shown while unverified" in error for error in errors))

    def test_source_reference_and_public_reference_are_independent(self):
        record, errors = self.clean()
        self.assertEqual(errors, [])
        self.assertEqual(record["source_reference"], "urbania:150921307")
        self.assertEqual(record["public_reference"], "ARZ-000001")
        self.assertNotIn("150921307", record["public_reference"])
        moved, errors = self.clean(source_reference="otroportal:999", source_url="https://otro.pe/999")
        self.assertEqual(errors, [])
        self.assertEqual(moved["public_reference"], "ARZ-000001")

    def test_the_three_areas_are_preserved_and_area_m2_stays_compatible(self):
        record, errors = self.clean()
        self.assertEqual(errors, [])
        self.assertEqual(record["area_total_m2"], 76.82)
        self.assertEqual(record["area_built_m2"], 58.75)
        self.assertEqual(record["terrace_area_m2"], 18.07)
        self.assertEqual(record["area_m2"], 76.82)

    def test_an_explicit_area_m2_is_not_overwritten_by_the_mirror(self):
        record, _ = importer.normalize_record(urbania(area_m2=58.75))
        self.assertEqual(record["area_m2"], 58.75)


if __name__ == "__main__":
    unittest.main()
