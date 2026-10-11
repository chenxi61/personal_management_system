# 个人成长档案系统 ER 图

```mermaid
erDiagram
    PROFILES ||--o{ RECORDS : 拥有
    CATEGORIES ||--o{ RECORDS : 分类
    RECORDS ||--o| CERTIFICATES : 附带

    PROFILES {
        INTEGER id PK "用户编号"
        TEXT name "姓名"
        TEXT created_at "创建时间"
    }

    CATEGORIES {
        INTEGER id PK "类型编号"
        TEXT name UK "类型名称"
        TEXT color "显示颜色"
    }

    RECORDS {
        INTEGER id PK "记录编号"
        INTEGER profile_id FK "所属用户"
        INTEGER category_id FK "记录类型"
        TEXT title "记录名称"
        TEXT notes "备注"
        TEXT record_date "记录日期"
        TEXT created_at "创建时间"
        TEXT updated_at "更新时间"
    }

    CERTIFICATES {
        INTEGER id PK "附件编号"
        INTEGER record_id FK_UK "关联记录"
        TEXT file_path "保存路径"
        TEXT original_name "原文件名"
        TEXT uploaded_at "上传时间"
    }
```

关系说明：

- 一个用户可以拥有多条成长记录。
- 一种记录类型可以对应多条成长记录。
- 一条成长记录可以没有证书，也可以关联一份 PDF 证书。
- 删除成长记录时，数据库通过级联关系自动删除对应的证书信息。
