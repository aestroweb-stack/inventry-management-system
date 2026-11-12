from datetime import datetime
from typing import Optional, List
from pydantic import BaseModel, ConfigDict, Field


class VendorOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    contact: Optional[str] = None


class VariantCreate(BaseModel):
    size: str
    initial_stock: int = 0
    reorder_threshold: int = 5
    target_stock_level: int = 20


class ProductCreate(BaseModel):
    sku: str
    name: str
    category: Optional[str] = None
    vendor_id: Optional[int] = None
    variants: List[VariantCreate] = Field(min_length=1)


class ProductUpdate(BaseModel):
    sku: Optional[str] = None
    name: Optional[str] = None
    category: Optional[str] = None
    vendor_id: Optional[int] = None


class VariantUpdate(BaseModel):
    reorder_threshold: Optional[int] = None
    target_stock_level: Optional[int] = None


class VariantOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    variant_code: str
    size: str
    current_stock: int
    reorder_threshold: int
    target_stock_level: int
    status: str  # computed: OK / LOW / OUT


class ProductOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    sku: str
    name: str
    category: Optional[str] = None
    image_path: Optional[str] = None
    images: List[str] = Field(default_factory=list)
    vendor: Optional[VendorOut] = None
    variants: List[VariantOut]
    total_stock: int


class StockAdjustment(BaseModel):
    variant_code: str
    # `new_count` preserves the original absolute-count API. `delta` supports
    # an explicit restock/remove operation without ambiguous client math.
    new_count: Optional[int] = None
    operation: str = "set"
    delta: Optional[int] = None
    note: Optional[str] = "manual recount"
    expected_current_stock: Optional[int] = None


class OrderLineResult(BaseModel):
    id: Optional[int] = None
    sku: str
    size: Optional[str] = None
    qty_ordered: int
    qty_fulfilled: int
    pending_qty: int = 0
    dispatchable_qty: int = 0
    remaining_stock: Optional[int] = None
    status: str
    product_name: Optional[str] = None
    note: Optional[str] = None


class OrderUploadResult(BaseModel):
    order_upload_id: int
    filename: str
    total_rows: int
    unique_skus: int
    fulfilled_count: int
    pending_count: int = 0
    insufficient_count: int
    not_found_count: int
    lines: List[OrderLineResult]
    new_alerts: List[str]   # variant codes newly alerted as part of this upload
    file_note: Optional[str] = None   # e.g. "no quantity column - each row counted as 1"


class OrderDispatchRequest(BaseModel):
    line_ids: List[int] = Field(min_length=1)


class AlertOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    variant_code: str
    sku: str
    size: str
    product_name: str
    alert_type: str
    stock_at_alert: int
    current_stock: Optional[int] = None
    created_at: datetime
    ordered_qty: Optional[int] = None
    required_qty: Optional[int] = None


class AlertDesignGroup(BaseModel):
    sku: str
    product_name: str
    alert_count: int
    highest_severity: str
    items: List[AlertOut]


class AlertGroupPage(BaseModel):
    total: int
    limit: int
    offset: int
    items: List[AlertDesignGroup]


class ReorderItemOut(BaseModel):
    sku: str
    size: str
    variant_code: str
    name: str
    current_stock: int
    ordered_qty: int
    dispatched_qty: int = 0
    required_qty: int
    image_path: Optional[str] = None
    is_new: bool = False


class ReorderPage(BaseModel):
    total: int
    limit: int
    offset: int
    items: List[ReorderItemOut]


class TransactionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    change_qty: int
    transaction_type: str
    reference: Optional[str] = None
    balance_after: int
    created_by: str
    created_at: datetime


class VariantLookupOut(BaseModel):
    variant_code: str
    sku: str
    size: str
    product_name: str
    current_stock: int
    counted: bool = True     # False = stock never counted yet (current_stock is a placeholder 0)
    image_path: Optional[str] = None


class VariantRowOut(BaseModel):
    """One flat row per (design, size) - what the Live Inventory table shows."""
    variant_code: str
    sku: str
    size: str
    product_name: str
    category: Optional[str] = None
    vendor: Optional[str] = None
    image_path: Optional[str] = None
    current_stock: int
    reorder_threshold: int
    target_stock_level: int
    status: str            # UNCOUNTED / OUT / LOW / OK
    last_counted_at: Optional[datetime] = None


class VariantPage(BaseModel):
    total: int
    limit: int
    offset: int
    items: List[VariantRowOut]


class SummaryOut(BaseModel):
    products: int
    stock_units: int = 0
    variants: int
    uncounted: int
    out: int
    low: int
    ok: int
    open_alerts: int


class BulkSetResult(BaseModel):
    rows_processed: int
    updated_count: int
    not_found_count: int
    not_found_sample: List[str]
    skipped_blank_count: int
    duplicate_rows_ignored: int
    created_products_count: int = 0
    created_variants_count: int = 0


class BulkProductRowError(BaseModel):
    row: int
    sku: Optional[str] = None
    message: str


class BulkProductImportResult(BaseModel):
    rows_processed: int
    products_created: int
    variants_created: int
    rows_skipped: int
    blank_rows: int
    errors: List[BulkProductRowError]
    products_merged: int = 0
    variants_merged: int = 0
    duplicate_rows_merged: int = 0
    free_size_rows: int = 0
    errors_truncated: bool = False


class TrashProductOut(BaseModel):
    sku: str
    name: str
    image_path: Optional[str] = None
    variant_count: int
    deleted_at: datetime


class TrashPage(BaseModel):
    total: int
    limit: int
    offset: int
    items: List[TrashProductOut]


class TransactionLogOut(BaseModel):
    id: int
    variant_code: str
    sku: str
    size: str
    product_name: str
    change_qty: int
    transaction_type: str
    reference: Optional[str] = None
    balance_after: int
    created_by: str
    created_at: datetime


class MoverOut(BaseModel):
    sku: str
    name: str
    image_path: Optional[str] = None
    units_moved: int


class DeleteResult(BaseModel):
    deleted: str
    trashed: bool = True
    sizes_removed: Optional[int] = None
    product_also_deleted: Optional[bool] = None


class RestoreResult(BaseModel):
    restored: str
    variants_restored: Optional[int] = None
