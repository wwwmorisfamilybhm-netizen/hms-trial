import json
import tempfile
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from patients.models import (
    Patient, PatientBill, ConsultationSession, PrescriptionItem, Medicine, MedicineBatch,
    StockMovement, PharmacyIssue, ClinicalOrder, ServiceDepartment, DiagnosticReport,
    ServiceDefinition, ServiceSession,
)
from .operations import receive_stock, reserve_issue, handover_issue, cancel_issue, adjust_stock, reallocate_issue, set_bill_status

User = get_user_model()


def staff(name, role):
    user = User.objects.create_user(username=name)
    user.profile.designation = role
    user.profile.save()
    return user


class PharmacyWorkflowTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.pharmacist = staff('pharmacist', 'pharmacy')
        cls.cashier = staff('cashier', 'registration_desk')
        cls.doctor = staff('prescriber', 'doctor')
        cls.service_user = staff('service', 'service_staff')
        cls.admin = staff('operations_admin', 'admin')
        cls.patient = Patient.objects.create(first_name='Workflow', uhid='OPS-TEST')
        cls.consultation = ConsultationSession.objects.create(patient=cls.patient, doctor=cls.doctor, visit_token='OPS-VISIT', status='in_progress')
        cls.medicine = Medicine.objects.create(name='Test medicine 10 mg Tablet', pack_size=10)
        cls.rx = PrescriptionItem.objects.create(consultation=cls.consultation, medicine=cls.medicine, medicine_name=cls.medicine.name, prescribed_quantity=12, released_at=timezone.now())

    def receipt(self, name='B1', days=40, packs=1, price='2.00', key=None):
        return receive_stock(user=self.pharmacist, medicine=self.medicine, batch_number=name, expiry_date=timezone.localdate()+timedelta(days=days), supplier='Test supplier', invoice_reference='OPENING-1', purchase_cost=Decimal('1.00'), selling_price=Decimal(price), packs=packs, request_key=key or uuid4())

    def issue(self, quantity=6, key=None):
        return reserve_issue(user=self.pharmacist, consultation=self.consultation, entries=[{'prescription_id':self.rx.pk, 'medicine_id':self.medicine.pk, 'quantity':quantity}], request_key=key or uuid4())

    def pay(self, issue):
        set_bill_status(user=self.cashier, bill=issue.bill, next_status='paid')

    def test_receipt_pack_conversion_idempotency_and_ledger(self):
        key=uuid4()
        batch=self.receipt(key=key)
        self.receipt(key=key)
        batch.refresh_from_db()
        self.assertEqual(batch.on_hand,10)
        self.assertEqual(StockMovement.objects.count(),1)
        with self.assertRaises(ValidationError):
            self.receipt(price='3.00')
        self.assertEqual(StockMovement.objects.count(),1)

    def test_fefo_reservation_then_paid_partial_and_final_handovers(self):
        later=self.receipt('later',60,price='3.00')
        sooner=self.receipt('sooner',20,price='2.00')
        expired=MedicineBatch.objects.create(medicine=self.medicine,batch_number='expired',expiry_date=timezone.localdate()-timedelta(days=1),supplier='S',invoice_reference='OLD',purchase_cost=1,selling_price=1,on_hand=50)
        key=uuid4()
        issue=self.issue(11,key)
        self.assertEqual(issue.bill.consultation_id, self.consultation.pk)
        self.assertEqual(self.issue(11,key).pk,issue.pk)
        sooner.refresh_from_db();later.refresh_from_db();expired.refresh_from_db()
        self.assertEqual((sooner.reserved,later.reserved,expired.reserved),(10,1,0))
        self.assertEqual(issue.bill.amount,Decimal('23.00'))
        with self.assertRaises(ValidationError): handover_issue(user=self.pharmacist,issue=issue)
        # Unrelated dues are displayed but do not block a paid pharmacy issue.
        PatientBill.objects.create(patient=self.patient,source='registration',description='Other debt',amount=100)
        self.pay(issue)
        handover_issue(user=self.pharmacist,issue=issue)
        handover_issue(user=self.pharmacist,issue=issue)
        self.rx.refresh_from_db();sooner.refresh_from_db();later.refresh_from_db()
        self.assertEqual((self.rx.dispensed_quantity,self.rx.reserved_quantity,self.rx.dispense_status),(11,0,'partial'))
        self.assertEqual((sooner.on_hand,later.on_hand),(0,9))
        self.assertEqual(StockMovement.objects.filter(kind='issue').count(),2)
        balance=self.issue(1);self.pay(balance);handover_issue(user=self.pharmacist,issue=balance)
        self.rx.refresh_from_db();self.assertEqual(self.rx.dispense_status,'dispensed')
        with self.assertRaises(ValidationError):set_bill_status(user=self.cashier,bill=issue.bill,next_status='pending')

    def test_insufficient_stock_and_overprescribing_rollback_everything(self):
        batch=self.receipt()
        with self.assertRaises(ValidationError):self.issue(11)
        self.assertFalse(PharmacyIssue.objects.exists());self.assertFalse(PatientBill.objects.exists())
        self.rx.refresh_from_db();batch.refresh_from_db()
        self.assertEqual((batch.reserved,self.rx.reserved_quantity),(0,0))
        with self.assertRaises(ValidationError):self.issue(13)
        self.assertFalse(PatientBill.objects.exists())
        self.issue(8)
        with self.assertRaises(ValidationError):self.issue(5)
        self.assertEqual(PharmacyIssue.objects.count(),1)

    def test_cancel_unpaid_releases_balances_and_blocks_payment(self):
        batch=self.receipt();issue=self.issue()
        cancel_issue(user=self.pharmacist,issue=issue);cancel_issue(user=self.pharmacist,issue=issue)
        batch.refresh_from_db();self.rx.refresh_from_db();issue.bill.refresh_from_db()
        self.assertEqual((batch.reserved,self.rx.reserved_quantity,issue.bill.status),(0,0,'cancelled'))
        with self.assertRaises(ValidationError):self.pay(issue)
        self.assertFalse(StockMovement.objects.filter(kind='issue').exists())

    def test_paid_issue_cannot_be_cancelled_or_deduct_expired_batch(self):
        batch=self.receipt();issue=self.issue();self.pay(issue)
        with self.assertRaises(ValidationError):cancel_issue(user=self.pharmacist,issue=issue)
        MedicineBatch.objects.filter(pk=batch.pk).update(expiry_date=timezone.localdate()-timedelta(days=1))
        with self.assertRaises(ValidationError):handover_issue(user=self.pharmacist,issue=issue)
        issue.refresh_from_db();self.assertEqual(issue.status,'awaiting_payment')
        replacement=self.receipt('replacement',60,price='9.00')
        reallocate_issue(user=self.pharmacist,issue=issue)
        self.assertEqual(issue.lines.first().unit_price,Decimal('2.00'))
        handover_issue(user=self.pharmacist,issue=issue)
        replacement.refresh_from_db();self.assertEqual(replacement.on_hand,4)

    def test_adjustment_cannot_consume_reserved_stock(self):
        batch=self.receipt();self.issue(6)
        with self.assertRaises(ValidationError):adjust_stock(user=self.pharmacist,batch=batch,quantity=-5,reason='Count',request_key=uuid4())
        key=uuid4();adjust_stock(user=self.pharmacist,batch=batch,quantity=-4,reason='Count',request_key=key)
        adjust_stock(user=self.pharmacist,batch=batch,quantity=-4,reason='Count',request_key=key)
        batch.refresh_from_db();self.assertEqual(batch.on_hand,6)
        movement=StockMovement.objects.first()
        with self.assertRaises(ValidationError):movement.delete()
        with self.assertRaises(ValidationError):StockMovement.objects.all().update(quantity=99)

    def test_pharmacy_pages_cashier_and_role_boundaries(self):
        self.receipt();issue=self.issue()
        self.client.force_login(self.pharmacist)
        for name in ['pharmacy_dashboard','pharmacy_queue','medicine_catalogue','pharmacy_receive','pharmacy_stock_report','pharmacy_adjust','pharmacy_dispense_history']:
            self.assertEqual(self.client.get(reverse(name)).status_code,200,name)
        self.assertEqual(self.client.get(reverse('pharmacy_consultation',args=[self.consultation.pk])).status_code,200)
        self.assertContains(self.client.get(reverse('pharmacy_issue',args=[issue.pk])),'Confirm',count=0)
        self.assertEqual(self.client.get(reverse('pharmacy_stock_report')+'?export=csv').status_code,200)
        self.assertEqual(self.client.get(reverse('pharmacy_billing')).status_code,403)
        self.client.force_login(self.cashier)
        self.assertContains(self.client.get(reverse('pharmacy_billing')),'Review items')
        self.assertContains(self.client.get(reverse('patient_bill_slip',args=[issue.bill_id])),self.medicine.name)
        self.assertEqual(self.client.post(reverse('bill_set_status',args=[issue.bill_id]),{'status':'paid'}).status_code,302)
        self.client.force_login(self.pharmacist)
        self.assertContains(self.client.get(reverse('pharmacy_issue',args=[issue.pk])),'Confirm medicine handover')
        self.client.force_login(self.service_user)
        self.assertEqual(self.client.get(reverse('pharmacy_dashboard')).status_code,403)
        self.assertEqual(self.client.post(reverse('opd_prescription_update',args=[self.rx.pk]),{}).status_code,403)

    def test_doctor_release_requires_quantity_and_preserves_ids(self):
        self.client.force_login(self.doctor)
        draft=ConsultationSession.objects.create(patient=self.patient,doctor=self.doctor)
        payload={'patient_id':self.patient.pk,'consultation_id':draft.pk,'action':'save_draft','prescriptions':[{'medicine_name':'New medicine','dose':'1 tablet','route':'Oral','frequency':'OD','duration':'3 days','instructions':''}]}
        response=self.client.post(reverse('save_consultation'),data=json.dumps(payload),content_type='application/json')
        self.assertEqual(response.status_code,200,response.content)
        saved=response.json()['prescriptions'][0];self.assertIsNone(saved['released_at'])
        payload['action']='send_pharmacy';payload['prescriptions']=response.json()['prescriptions']
        response=self.client.post(reverse('save_consultation'),data=json.dumps(payload),content_type='application/json')
        self.assertEqual(response.status_code,400)
        payload['prescriptions'][0]['prescribed_quantity']=3
        response=self.client.post(reverse('save_consultation'),data=json.dumps(payload),content_type='application/json')
        self.assertEqual(response.status_code,200,response.content)
        self.assertEqual(response.json()['prescriptions'][0]['id'],saved['id'])
        self.assertTrue(response.json()['prescriptions'][0]['released_at'])
        payload['prescriptions']=response.json()['prescriptions'];payload['prescriptions'][0]['dose']='changed'
        response=self.client.post(reverse('save_consultation'),data=json.dumps(payload),content_type='application/json')
        self.assertEqual(response.status_code,400)
        self.assertEqual(PrescriptionItem.objects.get(pk=saved['id']).dose,'1 tablet')

    def test_role_navigation_has_valid_brand_link(self):
        self.client.force_login(self.pharmacist)
        session=self.client.session;session['designation']='pharmacy';session.save()
        response=self.client.get(reverse('pharmacy_dashboard'))
        self.assertContains(response,'class="brand" href="/pharmacy/"')
        self.assertContains(response,'Medicine catalogue',count=1)

    def test_desk_follows_a_partial_prescription_through_handover(self):
        self.receipt(packs=2)
        self.client.force_login(self.pharmacist)
        desk = reverse("pharmacy_dashboard")
        detail = reverse("pharmacy_consultation", args=[self.consultation.pk])
        state = "?tab=prepare&q=OPS-TEST&page=1"
        self.assertEqual(self.client.get(desk).context["tab"], "ready")
        self.assertEqual(self.client.get(desk + state).context["records"].paginator.count, 1)
        submission = {"request_key": uuid4(), f"medicine_{self.rx.pk}": self.medicine.pk,
                      f"quantity_{self.rx.pk}": 7}
        result = self.client.post(detail + state, submission)
        self.assertEqual(result.status_code, 302)
        self.assertTrue(result.url.endswith(state))
        self.assertEqual(self.client.post(detail + state, submission).url, result.url)
        issue = PharmacyIssue.objects.get()
        self.assertEqual(self.client.get(desk, {"tab": "payment"}).context["records"][0], issue)
        self.pay(issue)
        self.assertEqual(self.client.get(desk).context["records"][0], issue)
        self.client.post(result.url, {"action": "handover"})
        self.assertContains(self.client.get(reverse("pharmacy_dispense_history"), {"q": "OPS-TEST"}), "Details / print")
        self.assertEqual(self.client.get(desk).context["records"].paginator.count, 0)
        self.assertEqual(self.client.get(desk + state).context["records"][0], self.consultation)
        self.issue(quantity=5)
        self.assertEqual(self.client.get(desk + state).context["records"].paginator.count, 0)
        self.assertEqual(self.client.get(desk, {"tab": "all"}).context["records"].paginator.count, 1)

    def test_low_stock_includes_unreceived_medicine_and_csv_respects_filter(self):
        self.receipt(packs=3)
        Medicine.objects.create(name="Unreceived item")
        self.client.force_login(self.pharmacist)
        self.assertEqual(self.client.get(reverse("pharmacy_dashboard")).context["low_count"], 1)
        url = reverse("pharmacy_stock_report")
        self.assertContains(self.client.get(url, {"filter": "low"}), "No batch received")
        csv = self.client.get(url, {"filter": "low", "export": "csv"})
        self.assertContains(csv, "Unreceived item")
        self.assertNotContains(csv, self.medicine.name)

    def test_waiting_issues_are_oldest_first_and_overdue_alert_drills_down(self):
        self.receipt(packs=2)
        older = self.issue(quantity=2)
        newer = self.issue(quantity=2)
        PharmacyIssue.objects.filter(pk=older.pk).update(created_at=timezone.now() - timedelta(days=2))
        self.client.force_login(self.pharmacist)
        url = reverse("pharmacy_dashboard")
        response = self.client.get(url, {"tab": "payment"})
        self.assertEqual(list(response.context["records"]), [older, newer])
        self.assertEqual(response.context["stale_count"], 1)
        self.assertEqual(list(self.client.get(url, {"tab": "payment", "stale": "1"}).context["records"]), [older])
        self.assertEqual(self.client.get(url, {"tab": "payment", "q": "absent"}).context["records"].paginator.count, 0)

    def test_legacy_match_and_preparation_error_keep_entered_values(self):
        self.receipt()
        rx = PrescriptionItem.objects.create(consultation=self.consultation, released_at=timezone.now(),
                                            prescribed_quantity=3, medicine_name=self.medicine.name)
        PrescriptionItem.objects.create(consultation=self.consultation, released_at=timezone.now(),
                                        medicine_name="Legacy entry")
        self.client.force_login(self.pharmacist)
        url = reverse("pharmacy_consultation", args=[self.consultation.pk])
        response = self.client.post(url, {"request_key": uuid4(), f"quantity_{rx.pk}": "4",
                                         f"medicine_{rx.pk}": str(self.medicine.pk)})
        self.assertContains(response, "Doctor clarification needed")
        row = next(item for item in response.context["rows"] if item.pk == rx.pk)
        self.assertEqual((row.selected_match, row.entered_quantity), (str(self.medicine.pk), "4"))
        self.assertContains(response, "exceeds the unreserved prescription balance")
        self.assertFalse(PharmacyIssue.objects.exists())

    def test_catalogue_search_and_receiving_forms_preserve_identity(self):
        self.client.force_login(self.doctor)
        response=self.client.get(reverse('drug_search_api'),{'q':'Test medicine'})
        self.assertEqual(response.json()['drugs'][0]['id'],self.medicine.pk)
        self.client.force_login(self.pharmacist)
        response=self.client.post(reverse('pharmacy_receive'),{'medicine':self.medicine.pk,'batch_number':'FORM','expiry_date':str(timezone.localdate()+timedelta(days=80)),'supplier':'S','invoice_reference':'COUNT','packs':2,'purchase_cost':'1.00','selling_price':'2.00','request_key':str(uuid4())})
        self.assertEqual(response.status_code,302)
        self.assertEqual(MedicineBatch.objects.get(batch_number='FORM').on_hand,20)
        response=self.client.post(reverse('medicine_edit',args=[self.medicine.pk]),{'name':self.medicine.name,'generic_name':'','strength':'','form':'Tablet','unit':'bottle','pack_size':5,'reorder_level':10,'is_active':'on'})
        self.assertContains(response,'Unit and pack size cannot change')

    def test_catalogue_cards_kpis_filters_and_save_flow(self):
        self.receipt(packs=2)
        unstocked=Medicine.objects.create(name='Unstocked syrup',form='Syrup',manufacturer='Test maker',barcode='8900000001')
        retired=Medicine.objects.create(name='Retired item',is_active=False)
        scarce=Medicine.objects.create(name='Scarce item',reorder_level=15)
        receive_stock(user=self.pharmacist,medicine=scarce,batch_number='SCARCE-1',expiry_date=timezone.localdate()+timedelta(days=30),supplier='Test supplier',invoice_reference='OPENING-SCARCE',purchase_cost=Decimal('1.00'),selling_price=Decimal('2.00'),packs=1,request_key=uuid4())
        self.client.force_login(self.pharmacist)
        url=reverse('medicine_catalogue')
        page=self.client.get(url)
        self.assertEqual(page.context['counts'],{'total':4,'active':3,'low':2,'unstocked':2,'expiring':2})
        self.assertContains(page,'tablets available')
        self.assertContains(page,'No stock yet')
        self.assertContains(page,'Expiring ≤ 90 d')
        self.assertContains(page,'is-inactive')
        # Search covers barcode and manufacturer, not just the name.
        self.assertContains(self.client.get(url,{'q':'8900000001'}),'Unstocked syrup')
        self.assertContains(self.client.get(url,{'q':'Test maker'}),'Unstocked syrup')
        self.assertNotContains(self.client.get(url,{'q':'8900000001'}),'Scarce item')
        # Status, stock and sort filters select the matching cards.
        inactive=self.client.get(url,{'status':'inactive'})
        self.assertContains(inactive,'Retired item')
        self.assertNotContains(inactive,'Test medicine 10 mg Tablet')
        self.assertContains(self.client.get(url,{'stock':'unstocked'}),'Retired item')
        low=self.client.get(url,{'stock':'low'})
        self.assertContains(low,'Scarce item')
        self.assertContains(low,'Unstocked syrup')
        self.assertEqual(self.client.get(url,{'sort':'recent'}).context['medicines'][0].pk,scarce.pk)
        # Editing carries the search context, locks identity and saves in place.
        edit_page=self.client.get(reverse('medicine_edit',args=[scarce.pk]))
        self.assertContains(edit_page,'identity fields are locked')
        self.assertContains(edit_page,'readonly')
        self.assertContains(edit_page,'type="hidden" name="unit"')
        self.assertContains(self.client.get(url,{'q':'Scarce'}),f'href="/pharmacy/catalogue/{scarce.pk}/?q=Scarce#medicine-form"')
        response=self.client.post(reverse('medicine_edit',args=[scarce.pk])+'?q=Scarce',
            {'name':'Scarce item','generic_name':'','strength':'','form':'Tablet','unit':scarce.unit,
             'pack_size':scarce.pack_size,'reorder_level':20,'is_active':'on','manufacturer':'','barcode':''})
        self.assertRedirects(response,reverse('medicine_catalogue')+f'?q=Scarce&saved={scarce.pk}')
        scarce.refresh_from_db()
        self.assertEqual((scarce.reorder_level,scarce.unit),(20,'tablet'))
        self.assertContains(self.client.get(url,{'q':'Scarce','saved':scarce.pk}),'is-saved')
        # Catalogue cards link to a prefilled receiving form.
        receive_page=self.client.get(reverse('pharmacy_receive'),{'medicine':self.medicine.pk})
        self.assertEqual(receive_page.context['form'].initial.get('medicine'),self.medicine.pk)
        self.assertIsNone(self.client.get(reverse('pharmacy_receive'),{'medicine':'absent'}).context['form'].initial.get('medicine'))

    def test_released_prescription_and_prepared_bill_snapshots_are_immutable(self):
        self.receipt();issue=self.issue()
        self.rx.dose='different'
        with self.assertRaises(ValidationError):self.rx.save()
        issue.bill.amount=Decimal('999')
        with self.assertRaises(ValidationError):issue.bill.save()
        self.assertEqual(PatientBill.objects.get(pk=issue.bill_id).amount,Decimal('12'))

    def test_doctor_can_cancel_only_unreserved_balance(self):
        self.receipt();issue=self.issue()
        self.client.force_login(self.doctor)
        url=reverse('prescription_cancel',args=[self.rx.pk])
        response=self.client.post(url,HTTP_ACCEPT='application/json')
        self.assertEqual(response.status_code,400)
        cancel_issue(user=self.pharmacist,issue=issue)
        response=self.client.post(url,HTTP_ACCEPT='application/json')
        self.assertEqual(response.status_code,200)
        self.rx.refresh_from_db();self.assertEqual(self.rx.dispense_status,'cancelled')

    def test_patient_with_issue_history_is_retained_on_delete_attempt(self):
        self.receipt();self.issue()
        self.admin.set_password('operations-test-password');self.admin.save()
        self.client.force_login(self.admin)
        response=self.client.post(reverse('patient_delete',args=[self.patient.pk]),{'password':'operations-test-password'})
        self.assertEqual(response.status_code,302)
        self.assertTrue(Patient.objects.filter(pk=self.patient.pk).exists())
        self.assertTrue(PharmacyIssue.objects.exists())

    def test_drafts_hidden_and_direct_dispense_cannot_bypass_workflow(self):
        self.client.force_login(self.pharmacist)
        draft=ConsultationSession.objects.create(patient=self.patient,doctor=self.doctor,visit_token='PRIVATE-DRAFT')
        rx=PrescriptionItem.objects.create(consultation=draft,medicine_name='Draft medicine')
        self.assertNotContains(self.client.get(reverse('pharmacy_queue')),'PRIVATE-DRAFT')
        self.client.post(reverse('opd_prescription_update',args=[rx.pk]),{'dispense_status':'dispensed','stock_status':'available','dispensed_quantity':3})
        rx.refresh_from_db();self.assertEqual(rx.dispensed_quantity,0)


class ServiceOperationsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.doctor=staff('service_doctor','doctor');cls.other=staff('other_doctor','doctor')
        cls.technician=staff('lab_technician','service_staff');cls.imaging=staff('imaging_technician','service_staff')
        cls.admin=staff('service_admin','admin');cls.pharmacist=staff('service_pharm','pharmacy')
        cls.lab=ServiceDepartment.objects.get(slug='laboratory');cls.radiology=ServiceDepartment.objects.get(slug='radiology')
        cls.technician.profile.service_departments.add(cls.lab);cls.imaging.profile.service_departments.add(cls.radiology)
        cls.patient=Patient.objects.create(first_name='Report patient',uhid='REPORT-TEST')
        cls.consultation=ConsultationSession.objects.create(patient=cls.patient,doctor=cls.doctor,visit_token='REPORT-VISIT')
        cls.order=ClinicalOrder.objects.create(consultation=cls.consultation,name='Test report',order_type='laboratory')

    def test_department_isolation_on_pages_updates_and_reports(self):
        self.client.force_login(self.imaging)
        self.assertEqual(self.client.get(reverse('opd_service_queue',args=['laboratory'])).status_code,403)
        self.assertEqual(self.client.post(reverse('opd_order_update',args=[self.order.pk]),{'status':'Completed'}).status_code,404)
        self.assertNotContains(self.client.get(reverse('service_staff_dashboard')),'Test report')
        self.client.force_login(self.technician)
        for name in ['service_staff_dashboard','service_completed_today','service_reports']:
            self.assertEqual(self.client.get(reverse(name)).status_code,200)
        self.assertContains(self.client.get(reverse('opd_service_queue',args=['laboratory'])+f'?order={self.order.pk}'),'Specimen / collection')

    def test_completion_timestamp_does_not_change_on_later_update(self):
        self.client.force_login(self.technician)
        url=reverse('opd_order_update',args=[self.order.pk])
        self.client.post(url,{'status':'Completed','specimen_status':'received'})
        self.order.refresh_from_db();timestamp=self.order.completed_at
        self.assertIsNotNone(timestamp)
        self.client.post(url,{'status':'Completed','result_summary':'Updated summary'})
        self.order.refresh_from_db();self.assertEqual(self.order.completed_at,timestamp)
        self.assertContains(self.client.get(reverse('service_completed_today')),'Test report')

    def test_report_publish_download_and_requesting_doctor_review(self):
        with tempfile.TemporaryDirectory() as report_dir, override_settings(PRIVATE_REPORT_ROOT=report_dir):
            self.client.force_login(self.technician)
            response=self.client.post(reverse('report_publish',args=[self.order.pk]),{'title':'Lab report','summary':'Test summary','attachment':SimpleUploadedFile('test.pdf',b'%PDF-1.4\nsynthetic test',content_type='application/pdf')})
            self.assertEqual(response.status_code,302)
            report=DiagnosticReport.objects.get(order=self.order)
            self.assertTrue(report.attachment.storage.exists(report.attachment.name))
            self.assertEqual(str(report.attachment.storage.location), report_dir)
            self.assertNotIn('/media/',str(report.attachment.storage.location))
            for user in [self.imaging,self.pharmacist,self.other]:
                self.client.force_login(user)
                self.assertEqual(self.client.get(reverse('report_download',args=[report.pk])).status_code,404)
            self.client.force_login(self.doctor)
            download=self.client.get(reverse('report_download',args=[report.pk]))
            self.assertEqual(download.status_code,200);download.close()
            self.assertContains(self.client.get(reverse('service_reports')),'Lab report')
            self.client.post(reverse('report_review',args=[report.pk]));report.refresh_from_db()
            timestamp=report.reviewed_at;self.assertEqual(report.reviewed_by,self.doctor)
            self.client.post(reverse('report_review',args=[report.pk]));report.refresh_from_db();self.assertEqual(report.reviewed_at,timestamp)
            self.assertContains(self.client.get(reverse('doctor_consultation')+f'?patient={self.patient.pk}'),'Download report')

    def test_invalid_report_rejected_and_admin_routes_unknown_order(self):
        self.client.force_login(self.technician)
        self.client.post(reverse('report_publish',args=[self.order.pk]),{'title':'Bad report','summary':'Invalid upload','attachment':SimpleUploadedFile('bad.pdf',b'not a PDF')})
        self.assertFalse(DiagnosticReport.objects.exists())
        unknown=ClinicalOrder.objects.create(consultation=self.consultation,name='Unclassified request',order_type='other')
        self.assertIsNone(unknown.department_id)
        self.client.force_login(self.admin)
        self.assertContains(self.client.get(reverse('service_setup')),'Unclassified request')
        self.client.post(reverse('service_setup'),{'action':'route','order_id':unknown.pk,'department_id':self.lab.pk})
        unknown.refresh_from_db();self.assertEqual(unknown.department_id,self.lab.pk)

    def test_administrator_assigns_departments_using_staff_form(self):
        self.client.force_login(self.admin)
        response=self.client.post(reverse('admin_doctor_edit',args=[self.imaging.pk]),{'first_name':'Imaging','username':self.imaging.username,'last_name':'','email':'','designation':'service_staff','specialty':'','password':'','service_departments':[self.lab.pk,self.radiology.pk]})
        self.assertEqual(response.status_code,302)
        self.assertEqual(self.imaging.profile.service_departments.count(),2)

    def test_report_does_not_leak_into_another_doctors_consultation_page(self):
        DiagnosticReport.objects.create(patient=self.patient,order=self.order,title='REQUESTING-DOCTOR-ONLY',summary='Private summary',published_at=timezone.now(),author=self.technician)
        self.client.force_login(self.other)
        response=self.client.get(reverse('doctor_consultation')+f'?patient={self.patient.pk}')
        self.assertNotContains(response,'REQUESTING-DOCTOR-ONLY')

    def test_service_navigation_lists_only_assigned_departments(self):
        self.client.force_login(self.technician)
        session=self.client.session;session['designation']='service_staff';session.save()
        response=self.client.get(reverse('service_staff_dashboard'))
        self.assertContains(response,'/opd-assistant/services/laboratory/')
        self.assertNotContains(response,'/opd-assistant/services/radiology/')

    def test_no_department_is_not_cross_department_access(self):
        person=staff('unassigned_staff','service_staff');self.client.force_login(person)
        self.assertContains(self.client.get(reverse('service_staff_dashboard')),'No departments assigned')
        self.assertEqual(self.client.get(reverse('opd_service_queue',args=['laboratory'])).status_code,403)

    def test_radiology_modality_tabs_filter_studies(self):
        xray=ServiceDefinition.objects.create(name='Chest X-Ray test',department=self.radiology,modality='xray')
        usg=ServiceDefinition.objects.create(name='USG test',department=self.radiology,modality='usg')
        ClinicalOrder.objects.create(consultation=self.consultation,name=xray.name,order_type='radiology',service_definition=xray)
        ClinicalOrder.objects.create(consultation=self.consultation,name=usg.name,order_type='radiology',service_definition=usg)
        self.client.force_login(self.imaging)
        usg_page=self.client.get(reverse('opd_service_queue',args=['radiology']),{'modality':'usg'})
        self.assertContains(usg_page,'USG test')
        self.assertNotContains(usg_page,'Chest X-Ray test')
        xray_page=self.client.get(reverse('opd_service_queue',args=['radiology']),{'modality':'xray'})
        self.assertContains(xray_page,'Chest X-Ray test')
        self.assertNotContains(xray_page,'USG test')
        self.assertContains(usg_page,'Unspecified')

    def test_lab_collection_stamp_and_critical_flag(self):
        self.client.force_login(self.technician)
        self.client.post(reverse('opd_order_update',args=[self.order.pk]),{
            'status':'In Progress','specimen_status':'collected','critical_flag':'on',
        })
        self.order.refresh_from_db()
        self.assertEqual(self.order.status,'In Progress')
        self.assertTrue(self.order.operational_details['critical_flag'])
        self.assertTrue(self.order.operational_details['collected_at'])
        DiagnosticReport.objects.create(patient=self.patient,order=self.order,title='Critical lab report',summary='Call the doctor',published_at=timezone.now(),author=self.technician)
        self.client.force_login(self.doctor)
        self.assertContains(self.client.get(reverse('service_reports')),'Critical result')

    def test_physiotherapy_plan_auto_completes_after_planned_sessions(self):
        physio=ServiceDepartment.objects.get(slug='physiotherapy')
        self.technician.profile.service_departments.add(physio)
        order=ClinicalOrder.objects.create(consultation=self.consultation,department=physio,order_type='other',name='Physiotherapy plan',sessions_planned=2)
        self.client.force_login(self.technician)
        self.client.post(reverse('service_session_add',args=[order.pk]),{'scheduled_at':'2026-11-21T10:00','room':'PT-1'})
        self.assertEqual(order.sessions.count(),1)
        first=order.sessions.first()
        self.client.post(reverse('service_session_update',args=[first.pk]),{'status':'attended','progress_note':'Synthetic progress'})
        order.refresh_from_db();self.assertEqual(order.status,'In Progress')
        self.assertIsNone(order.completed_at)
        second=ServiceSession.objects.create(order=order,scheduled_at=timezone.now())
        self.client.post(reverse('service_session_update',args=[second.pk]),{'status':'attended'})
        order.refresh_from_db()
        self.assertEqual(order.status,'Completed')
        self.assertIsNotNone(order.completed_at)
        self.assertEqual(order.completed_by,self.technician)
        self.assertContains(self.client.get(reverse('service_completed_today')),'Physiotherapy plan')

    def test_counselling_notes_are_private_and_not_in_audit(self):
        counselling=ServiceDepartment.objects.get(slug='counselling')
        order=ClinicalOrder.objects.create(consultation=self.consultation,department=counselling,order_type='other',name='Counselling plan',sessions_planned=1)
        opd=staff('service_opd','opd_assistant')
        self.technician.profile.service_departments.add(counselling)
        self.client.force_login(self.technician)
        self.client.post(reverse('service_session_add',args=[order.pk]),{'scheduled_at':'2026-11-22T09:00','progress_note':'SENSITIVE-COUNSELLING-NOTE'})
        session=order.sessions.get()
        self.assertEqual(session.progress_note,'SENSITIVE-COUNSELLING-NOTE')
        for event in order.audit_events.all():
            self.assertNotIn('SENSITIVE-COUNSELLING-NOTE',event.summary)
            self.assertNotIn('SENSITIVE-COUNSELLING-NOTE',str(event.details))
        self.client.force_login(opd)
        page=self.client.get(reverse('opd_service_queue',args=['counselling']),{'order':order.pk})
        self.assertContains(page,'Clinician-only note')
        self.assertNotContains(page,'SENSITIVE-COUNSELLING-NOTE')

    def test_unpaid_session_plan_and_other_department_are_blocked(self):
        physio=ServiceDepartment.objects.get(slug='physiotherapy')
        self.technician.profile.service_departments.add(physio)
        bill=PatientBill.objects.create(patient=self.patient,source='service',description='Physiotherapy',amount=500)
        order=ClinicalOrder.objects.create(consultation=self.consultation,department=physio,order_type='other',name='Unpaid physiotherapy',sessions_planned=1,bill=bill)
        self.client.force_login(self.technician)
        self.client.post(reverse('service_session_add',args=[order.pk]),{'scheduled_at':'2026-11-23T10:00'})
        self.assertFalse(ServiceSession.objects.filter(order=order).exists())
        self.client.force_login(self.imaging)
        self.assertEqual(self.client.post(reverse('service_session_add',args=[order.pk]),{'scheduled_at':'2026-11-23T10:00'}).status_code,404)


class PharmacyConcurrencyTests(TransactionTestCase):
    def test_two_counters_cannot_reserve_the_same_stock(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        from django.db import close_old_connections, OperationalError, IntegrityError
        pharmacist=staff('concurrent_pharmacist','pharmacy')
        patient=Patient.objects.create(first_name='Concurrency')
        medicine=Medicine.objects.create(name='Concurrent medicine')
        batch=MedicineBatch.objects.create(medicine=medicine,batch_number='ONLY',expiry_date=timezone.localdate()+timedelta(days=10),supplier='Test',invoice_reference='OPEN',purchase_cost=1,selling_price=2,on_hand=5)
        consultation=ConsultationSession.objects.create(patient=patient,status='in_progress')
        prescriptions=[PrescriptionItem.objects.create(consultation=consultation,medicine=medicine,medicine_name=medicine.name,prescribed_quantity=5,released_at=timezone.now()) for _ in range(2)]
        barrier=Barrier(2)
        keys=[uuid4(),uuid4()]
        def reserve(index):
            close_old_connections()
            actor=User.objects.get(pk=pharmacist.pk)
            visit=ConsultationSession.objects.get(pk=consultation.pk)
            barrier.wait()
            try:
                result=reserve_issue(user=actor,consultation=visit,entries=[{'prescription_id':prescriptions[index].pk,'medicine_id':medicine.pk,'quantity':5}],request_key=keys[index])
                return result.pk
            except (ValidationError,OperationalError,IntegrityError):
                return None
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(reserve,range(2)))
        # SQLite may reject both contending transactions; a retry must still allow only one winner.
        for index,result in enumerate(results):
            if result is None:
                try:reserve_issue(user=pharmacist,consultation=consultation,entries=[{'prescription_id':prescriptions[index].pk,'medicine_id':medicine.pk,'quantity':5}],request_key=keys[index])
                except ValidationError:pass
        batch.refresh_from_db()
        self.assertEqual(batch.reserved,5)
        self.assertEqual(PharmacyIssue.objects.count(),1)
        self.assertEqual(PatientBill.objects.count(),1)
        self.assertEqual(batch.on_hand,5)
