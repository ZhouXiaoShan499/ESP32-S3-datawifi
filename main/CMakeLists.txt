idf_component_register(SRCS "main.c"
                    INCLUDE_DIRS "."
                    REQUIRES
                        json
                        esp_http_client
                        esp_netif
                        esp_wifi
                        esp_event
                        nvs_flash
                        sdmmc
                        fatfs
                        esp_timer
                        driver
                        esp_driver_i2c
                        esp_driver_gpio
                        esp_driver_sdmmc
                        esp_driver_sdspi
                        esp_lcd
                        vfs
                        esp_partition
                        wear_levelling
                        spi_flash
                        esp_system
                        hal
                        soc)
