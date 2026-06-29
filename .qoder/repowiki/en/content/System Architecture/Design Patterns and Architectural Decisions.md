# Design Patterns and Architectural Decisions

<cite>
**Referenced Files in This Document**
- [00_run_pipeline.py](file://00_run_pipeline.py)
- [01_data_acquisition.py](file://01_data_acquisition.py)
- [02_preprocess.py](file://02_preprocess.py)
- [03_feature_engineer.py](file://03_feature_engineer.py)
- [04_ai_predict.py](file://04_ai_predict.py)
- [05_save_alert_report.py](file://05_save_alert_report.py)
- [pipeline_utils.py](file://pipeline_utils.py)
- [config.yaml](file://config.yaml)
- [README.md](file://README.md)
</cite>

## Table of Contents
1. [Introduction](#introduction)
2. [Project Structure](#project-structure)
3. [Core Architectural Patterns](#core-architectural-patterns)
4. [Factory Pattern Implementation](#factory-pattern-implementation)
5. [Strategy Pattern Implementation](#strategy-pattern-implementation)
6. [Observer Pattern Implementation](#observer-pattern-implementation)
7. [Pipeline Pattern Implementation](#pipeline-pattern-implementation)
8. [Pattern Benefits and System Design](#pattern-benefits-and-system-design)
9. [Code-Level Pattern Analysis](#code-level-pattern-analysis)
10. [Conclusion](#conclusion)

## Introduction

The Aditya-L1 Solar Flare Forecasting Pipeline demonstrates sophisticated software architecture through the strategic implementation of multiple design patterns. This system processes real-time solar data from multiple sources, performs complex machine learning inference, and generates actionable alerts for space weather monitoring. The architecture emphasizes modularity, testability, and maintainability through well-defined patterns that enable easy model replacement, intelligent fallback mechanisms, and robust notification systems.

The pipeline operates as a continuous 5-minute cron job, processing data through eight sequential stages while maintaining resilience through configurable retry mechanisms and comprehensive error handling.

## Project Structure

The pipeline follows a modular, stage-based architecture with clear separation of concerns:

```mermaid
graph TB
subgraph "Pipeline Entry Point"
Master[00_run_pipeline.py]
end
subgraph "Data Layer"
Acquire[01_data_acquisition.py]
Preprocess[02_preprocess.py]
Features[03_feature_engineer.py]
Predict[04_ai_predict.py]
end
subgraph "Output Layer"
SaveReport[05_save_alert_report.py]
end
subgraph "Utilities"
Utils[pipeline_utils.py]
Config[config.yaml]
end
Master --> Acquire
Acquire --> Preprocess
Preprocess --> Features
Features --> Predict
Predict --> SaveReport
Master -.-> Utils
Acquire -.-> Utils
Preprocess -.-> Utils
Features -.-> Utils
Predict -.-> Utils
SaveReport -.-> Utils
Utils -.-> Config
```

**Diagram sources**
- [00_run_pipeline.py:63-146](file://00_run_pipeline.py#L63-L146)
- [01_data_acquisition.py:350-458](file://01_data_acquisition.py#L350-L458)
- [02_preprocess.py:230-422](file://02_preprocess.py#L230-L422)
- [03_feature_engineer.py:199-265](file://03_feature_engineer.py#L199-L265)
- [04_ai_predict.py:402-466](file://04_ai_predict.py#L402-L466)
- [05_save_alert_report.py:452-507](file://05_save_alert_report.py#L452-L507)

**Section sources**
- [README.md:7-32](file://README.md#L7-L32)
- [00_run_pipeline.py:13-24](file://00_run_pipeline.py#L13-L24)

## Core Architectural Patterns

The system implements four primary design patterns that work together to create a robust, maintainable forecasting pipeline:

### 1. Factory Pattern for Model Loading Abstraction
The system uses a factory-like approach to dynamically load and configure machine learning models based on configuration and availability.

### 2. Strategy Pattern for Dual Data Acquisition
Intelligent fallback mechanism between PRADAN (native ISRO data) and NOAA SWPC (public fallback) data sources.

### 3. Observer Pattern for Multi-Channel Alert Notifications
Decoupled alert system that notifies multiple channels (email, webhooks, logs) without tightly coupling the alert logic to the main processing flow.

### 4. Pipeline Pattern for Sequential Processing
Structured processing stages with clear data flow and error handling between each stage.

## Factory Pattern Implementation

The Factory pattern is implemented through the `EnsemblePredictor` class in the AI prediction stage, which dynamically loads and configures different model types based on configuration and availability.

```mermaid
classDiagram
class EnsemblePredictor {
-lstm_model : LSTMFlareModel
-gru_model : GRUFlareModel
-trans_model : TransformerFlareModel
-surrogate : PhysicsSurrogate
-xgb_surr : XGBoostSurrogate
+predict_one_model(name, feat) np.ndarray
+predict(feat) dict
}
class LSTMFlareModel {
+forward(x) Tensor
}
class GRUFlareModel {
+forward(x) Tensor
}
class TransformerFlareModel {
+forward(x) Tensor
}
class PhysicsSurrogate {
+predict(feat_vec, raw) np.ndarray
}
class XGBoostSurrogate {
+predict_flare_prob(feat_vec) float
+predict(feat_vec, raw) np.ndarray
}
EnsemblePredictor --> LSTMFlareModel : "loads if available"
EnsemblePredictor --> GRUFlareModel : "loads if available"
EnsemblePredictor --> TransformerFlareModel : "loads if available"
EnsemblePredictor --> PhysicsSurrogate : "fallback"
EnsemblePredictor --> XGBoostSurrogate : "fallback"
```

**Diagram sources**
- [04_ai_predict.py:246-396](file://04_ai_predict.py#L246-L396)
- [04_ai_predict.py:64-127](file://04_ai_predict.py#L64-L127)
- [04_ai_predict.py:134-238](file://04_ai_predict.py#L134-L238)

The factory implementation provides:

- **Configuration-driven model selection**: Models are loaded based on configuration file settings
- **Graceful degradation**: When trained weights are unavailable, surrogate models are used
- **Easy model replacement**: New models can be added by extending the factory interface
- **Resource-aware loading**: Models are only loaded when dependencies are available

**Section sources**
- [04_ai_predict.py:246-309](file://04_ai_predict.py#L246-L309)
- [04_ai_predict.py:113-127](file://04_ai_predict.py#L113-L127)
- [config.yaml:66-77](file://config.yaml#L66-L77)

## Strategy Pattern Implementation

The Strategy pattern is implemented in the data acquisition stage, providing intelligent fallback mechanisms between PRADAN (native ISRO data) and NOAA SWPC (public fallback) data sources.

```mermaid
sequenceDiagram
participant Master as Pipeline Master
participant Acquire as DataAcquisition
participant PRADAN as PRADANClient
participant NOAA as NOAAFallback
participant State as PipelineState
Master->>Acquire : run()
Acquire->>PRADAN : login()
PRADAN-->>Acquire : login_ok?
alt PRADAN Available
Acquire->>PRADAN : fetch_instrument_files()
PRADAN-->>Acquire : files[]
loop For each instrument
Acquire->>PRADAN : download_fits()
PRADAN-->>Acquire : file_path
Acquire->>PRADAN : parse_fits()
PRADAN-->>Acquire : parsed_data
Acquire->>State : record_checksum()
end
Acquire-->>Master : PRADAN_L1_FITS
else PRADAN Unavailable
Acquire->>NOAA : fetch_xray()
NOAA-->>Acquire : xray_data
Acquire->>NOAA : fetch_kp()
NOAA-->>Acquire : kp_data
Acquire->>NOAA : fetch_solar_wind()
NOAA-->>Acquire : wind_data
Acquire->>State : record_checksum()
Acquire-->>Master : NOAA_SWPC_FALLBACK
end
```

**Diagram sources**
- [01_data_acquisition.py:350-458](file://01_data_acquisition.py#L350-L458)
- [01_data_acquisition.py:366-434](file://01_data_acquisition.py#L366-L434)

Key strategy implementations:

- **Dual acquisition strategies**: Native PRADAN data vs. NOAA fallback
- **Intelligent fallback logic**: Automatic switching based on availability
- **Configurable timeouts and retries**: Robust network handling
- **Checksum-based deduplication**: Prevents processing duplicate data

**Section sources**
- [01_data_acquisition.py:45-193](file://01_data_acquisition.py#L45-L193)
- [01_data_acquisition.py:199-325](file://01_data_acquisition.py#L199-L325)
- [01_data_acquisition.py:350-458](file://01_data_acquisition.py#L350-L458)

## Observer Pattern Implementation

The Observer pattern is implemented through the alert system, which notifies multiple channels (email, webhooks, logs) without tightly coupling the alert logic to the main processing flow.

```mermaid
classDiagram
class AlertEngine {
-THRESHOLDS : dict
+evaluate(pred, pred_id) list[dict]
+dispatch(alert) void
-send_email(alert, ch) void
-send_webhook(alert, ch) void
}
class PostgresWriter {
-conn : Connection
+insert_prediction(pred) str
+insert_alert(alert) void
+connect() bool
}
class EmailChannel {
+enabled : bool
+recipients : list[str]
+smtp_host : str
}
class WebhookChannel {
+enabled : bool
+url : str
}
class LogChannel {
+enabled : bool
}
AlertEngine --> EmailChannel : "notifies"
AlertEngine --> WebhookChannel : "notifies"
AlertEngine --> LogChannel : "notifies"
AlertEngine --> PostgresWriter : "persists"
```

**Diagram sources**
- [05_save_alert_report.py:222-298](file://05_save_alert_report.py#L222-L298)
- [05_save_alert_report.py:47-216](file://05_save_alert_report.py#L47-L216)

The observer implementation provides:

- **Multi-channel notification**: Email, webhooks, and console logging
- **Configurable alert thresholds**: Easy adjustment of sensitivity levels
- **Decoupled alert evaluation**: Separate from prediction logic
- **Persistent alert storage**: PostgreSQL backend for alert history

**Section sources**
- [05_save_alert_report.py:222-298](file://05_save_alert_report.py#L222-L298)
- [05_save_alert_report.py:47-216](file://05_save_alert_report.py#L47-L216)
- [config.yaml:79-89](file://config.yaml#L79-L89)

## Pipeline Pattern Implementation

The Pipeline pattern organizes the entire forecasting process into sequential stages with clear data flow and error handling between each stage.

```mermaid
flowchart TD
Start([Pipeline Start]) --> Step1["Data Acquisition<br/>01_data_acquisition.py"]
Step1 --> Step2["Preprocessing<br/>02_preprocess.py"]
Step2 --> Step3["Feature Engineering<br/>03_feature_engineer.py"]
Step3 --> Step4["AI Prediction<br/>04_ai_predict.py"]
Step4 --> Step5["Save & Alert<br/>05_save_alert_report.py"]
Step5 --> End([Pipeline Complete])
Step1 --> Error1{"Error?"}
Error1 --> |Yes| Retry1["Retry with Configurable Delay"]
Retry1 --> Error1
Error1 --> |No| Step2
Step2 --> Error2{"Error?"}
Error2 --> |Yes| Abort["Abort Pipeline"]
Error2 --> |No| Step3
Step3 --> Error3{"Error?"}
Error3 --> |Yes| Abort
Error3 --> |No| Step4
Step4 --> Error4{"Error?"}
Error4 --> |Yes| Abort
Error4 --> |No| Step5
Step5 --> Error5{"Error?"}
Error5 --> |Yes| Abort
Error5 --> |No| End
```

**Diagram sources**
- [00_run_pipeline.py:41-146](file://00_run_pipeline.py#L41-L146)

The pipeline implementation ensures:

- **Sequential processing**: Each stage depends on the previous stage's output
- **Configurable retries**: Built-in retry mechanism with exponential backoff
- **Comprehensive error handling**: Graceful degradation and failure reporting
- **State persistence**: Pipeline state maintained between runs

**Section sources**
- [00_run_pipeline.py:41-146](file://00_run_pipeline.py#L41-L146)
- [00_run_pipeline.py:63-121](file://00_run_pipeline.py#L63-L121)

## Pattern Benefits and System Design

The combination of these design patterns provides several key benefits:

### Modularity
- Each stage operates independently with well-defined interfaces
- Components can be tested and debugged in isolation
- Easy addition of new processing stages

### Testability
- Factory pattern enables mocking of model loading
- Strategy pattern allows testing of different data sources
- Observer pattern facilitates unit testing of alert systems

### Maintainability
- Configuration-driven model selection reduces code changes
- Clear separation of concerns simplifies updates
- Centralized error handling and logging

### Scalability
- Factory pattern supports multiple model types
- Strategy pattern accommodates new data sources
- Pipeline pattern enables parallel processing where appropriate

## Code-Level Pattern Analysis

### Factory Pattern Details

The factory implementation in the AI prediction stage demonstrates sophisticated dependency injection and resource management:

```mermaid
classDiagram
class EnsemblePredictor {
+__init__()
+predict_one_model(name, feat) np.ndarray
+predict(feat) dict
}
class ModelLoader {
+load_torch_model(model_class, model_path) Model
+load_xgb_model(model_path) Booster
}
class SurrogateModels {
+PhysicsSurrogate
+XGBoostSurrogate
}
EnsemblePredictor --> ModelLoader : "uses"
EnsemblePredictor --> SurrogateModels : "fallbacks"
```

**Diagram sources**
- [04_ai_predict.py:246-396](file://04_ai_predict.py#L246-L396)
- [04_ai_predict.py:113-127](file://04_ai_predict.py#L113-L127)

### Strategy Pattern Details

The dual acquisition strategy provides intelligent fallback mechanisms:

```mermaid
flowchart LR
PRADAN[PRADAN Client] --> Decision{Credentials Available?}
Decision --> |Yes| Native[Native PRADAN Data]
Decision --> |No| NOAA[NOAA Fallback]
Native --> Validation[Data Validation]
NOAA --> Proxy[Proxy Data Processing]
Validation --> Merge[Combined Processing]
Proxy --> Merge
Merge --> Output[Final Dataset]
```

**Diagram sources**
- [01_data_acquisition.py:366-434](file://01_data_acquisition.py#L366-L434)

### Observer Pattern Details

The alert system demonstrates event-driven architecture:

```mermaid
sequenceDiagram
participant Predictor as Prediction Engine
participant AlertEngine as Alert Engine
participant Email as Email Channel
participant Webhook as Webhook Channel
participant Database as PostgreSQL
Predictor->>AlertEngine : evaluate(prediction)
AlertEngine->>AlertEngine : check thresholds
AlertEngine->>Database : insert alert
AlertEngine->>Email : send notification
AlertEngine->>Webhook : post alert
AlertEngine-->>Predictor : alert list
```

**Diagram sources**
- [05_save_alert_report.py:222-298](file://05_save_alert_report.py#L222-L298)

## Conclusion

The Aditya-L1 Solar Flare Forecasting Pipeline exemplifies modern software architecture through the strategic implementation of four key design patterns. The Factory pattern enables flexible model loading and configuration-driven selection, while the Strategy pattern provides intelligent fallback mechanisms for data acquisition. The Observer pattern creates a decoupled alert notification system, and the Pipeline pattern organizes the entire processing workflow into maintainable stages.

These patterns work together to create a system that is highly modular, testable, maintainable, and scalable. The architecture supports easy model replacement, robust error handling, and extensible notification capabilities, making it suitable for production space weather monitoring operations.

The implementation demonstrates how design patterns can be adapted to real-world scenarios, providing both theoretical benefits and practical advantages in a mission-critical system. The codebase serves as an excellent example of how thoughtful architectural decisions can create systems that are both powerful and maintainable.